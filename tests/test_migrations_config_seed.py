import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.config import DEV_KEY_VALUE, ConfigError, hash_key, load_dotenv, load_settings, parse_api_keys
from app.db import metadata, normalise_database_url
from app.main import create_app
from app.migrate import MIGRATIONS_DIR, MigrationError, apply_migrations, discover, pending_migrations, render
from app.normalizers import normalize
from app.seed import load_seed_events, seed

# ---------------------------------------------------------------- migrations


def test_migrations_apply_once_and_in_order(raw_engine):
    assert [m.version for m in pending_migrations(raw_engine)] == ["001", "002", "003"]
    applied = apply_migrations(raw_engine)
    assert [m.version for m in applied] == ["001", "002", "003"]
    assert apply_migrations(raw_engine) == []
    assert pending_migrations(raw_engine) == []


def test_migrated_schema_matches_table_definitions(engine):
    """The SQL files and the SQLAlchemy tables must describe the same columns."""
    inspector = inspect(engine)
    for table in metadata.sorted_tables:
        in_database = {column["name"]: column for column in inspector.get_columns(table.name)}
        assert set(in_database) == {column.name for column in table.columns}, table.name
        for column in table.columns:
            assert in_database[column.name]["nullable"] == column.nullable, f"{table.name}.{column.name}"
    unique = inspector.get_unique_constraints("records")
    assert any(set(u["column_names"]) == {"source", "external_id"} for u in unique)


def test_edited_migration_is_refused(raw_engine, tmp_path):
    directory = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, directory)
    apply_migrations(raw_engine, directory)
    target = directory / "001_records.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- sneaky edit\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="edited after it was applied"):
        apply_migrations(raw_engine, directory)


def test_checksum_ignores_line_endings(tmp_path):
    unix = tmp_path / "unix"
    windows = tmp_path / "windows"
    unix.mkdir()
    windows.mkdir()
    (unix / "001_a.sql").write_bytes(b"CREATE TABLE a (id TEXT);\n")
    (windows / "001_a.sql").write_bytes(b"CREATE TABLE a (id TEXT);\r\n")
    assert discover(unix)[0].checksum == discover(windows)[0].checksum


def test_bad_migration_file_name_is_refused(tmp_path):
    (tmp_path / "first.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError):
        discover(tmp_path)


def test_render_fills_placeholders_per_dialect():
    sql = "-- comment\nCREATE TABLE t (at {{TIMESTAMP}}, data {{JSON}});\nCREATE INDEX i ON t (at);\n"
    assert render(sql, "sqlite") == ["CREATE TABLE t (at TIMESTAMP, data TEXT)", "CREATE INDEX i ON t (at)"]
    assert render(sql, "postgresql")[0] == "CREATE TABLE t (at TIMESTAMPTZ, data JSONB)"
    with pytest.raises(MigrationError):
        render(sql, "oracle")


def test_readyz_is_503_until_migrated(settings, raw_engine):
    with TestClient(create_app(settings, raw_engine)) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["database"]["ok"] is True
        assert body["checks"]["migrations"]["pending"] == [
            "001_records",
            "002_jobs_and_outbox",
            "003_record_enrichment",
        ]
        assert client.get("/healthz").status_code == 200  # liveness is unaffected
        apply_migrations(raw_engine)
        assert client.get("/readyz").status_code == 200


# ---------------------------------------------------------------- config


def test_dev_falls_back_to_dev_key_and_production_refuses():
    dev = load_settings({})
    assert dev.using_dev_key and dev.api_keys == {"dev": hash_key(DEV_KEY_VALUE)}
    with pytest.raises(ConfigError, match="OPS_API_KEYS"):
        load_settings({"APP_ENV": "production"})
    prod = load_settings({"APP_ENV": "production", "OPS_API_KEYS": "crm:a-long-enough-key"})
    assert prod.is_production and not prod.using_dev_key


def test_parse_api_keys_plain_and_prehashed():
    digest = hash_key("another-long-key")
    keys = parse_api_keys(f"crm:first-long-key-1, forms:sha256:{digest}")
    assert keys == {"crm": hash_key("first-long-key-1"), "forms": digest}
    assert "first-long-key-1" not in repr(keys)


@pytest.mark.parametrize(
    "raw",
    ["nameonly", "crm:short", "crm:sha256:nothex", "crm:first-long-key-1,crm:second-long-key-2"],
)
def test_parse_api_keys_rejects_bad_entries(raw):
    with pytest.raises(ConfigError):
        parse_api_keys(raw)


def test_settings_parsing_and_validation():
    settings = load_settings(
        {"WORKER_ENABLED": "no", "WORKER_POLL_SECONDS": "0.5", "PORT": "9001", "LOG_FORMAT": "text"}
    )
    assert settings.worker_enabled is False
    assert settings.worker_poll_seconds == 0.5 and settings.port == 9001 and settings.log_format == "text"
    with pytest.raises(ConfigError):
        load_settings({"LOG_FORMAT": "xml"})
    with pytest.raises(ConfigError):
        load_settings({"WORKER_POLL_SECONDS": "soon"})


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nFOS_TEST_A=from-file\nFOS_TEST_B='quoted value'\n", encoding="utf-8")
    monkeypatch.setenv("FOS_TEST_A", "from-process")
    monkeypatch.delenv("FOS_TEST_B", raising=False)
    loaded = load_dotenv(env_file)
    import os

    assert loaded == ["FOS_TEST_B"]
    assert os.environ["FOS_TEST_A"] == "from-process"
    assert os.environ["FOS_TEST_B"] == "quoted value"
    monkeypatch.delenv("FOS_TEST_B")
    assert load_dotenv(tmp_path / "missing.env") == []


def test_postgres_urls_get_the_psycopg_driver():
    assert normalise_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_database_url("sqlite:///./data/ops.db") == "sqlite:///./data/ops.db"


# ---------------------------------------------------------------- seed data


def test_seed_file_is_valid_and_obviously_fake():
    events = load_seed_events()
    assert len(events) == 24
    assert {entry["source"] for entry in events} == {"webform", "sms", "call", "email"}
    for entry in events:
        event = normalize(entry["source"], entry["payload"])
        if event.contact_phone:
            assert event.contact_phone.startswith("+120255501"), event.contact_phone
        if event.contact_email:
            assert event.contact_email.endswith("example.com"), event.contact_email


def test_seed_is_idempotent_and_flows_through_jobs(engine, settings):
    from app.jobs import Worker
    from app.notify import MockNotifier

    assert seed(engine) == {"created": 24, "duplicate": 0}
    assert seed(engine) == {"created": 0, "duplicate": 24}
    notifier = MockNotifier()
    ran = Worker(engine, settings, notifier).drain()
    assert ran == 24 + len(notifier.sent)
    assert len(notifier.sent) >= 5
