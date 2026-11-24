"""Shared fixtures.

By default every test gets its own SQLite file. Set TEST_DATABASE_URL to run
the same suite against Postgres (the CI workflow does this in a second job).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.db import make_engine  # noqa: E402
from app.jobs import Worker  # noqa: E402
from app.main import create_app  # noqa: E402
from app.migrate import apply_migrations  # noqa: E402
from app.notify import MockNotifier  # noqa: E402

TEST_KEY = "test-key-not-a-secret"
AUTH = {"X-API-Key": TEST_KEY}


@pytest.fixture
def database_url(tmp_path) -> str:
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def settings(database_url):
    return load_settings(
        {
            "APP_ENV": "test",
            "DATABASE_URL": database_url,
            "OPS_API_KEYS": f"tests:{TEST_KEY}",
            "WORKER_ENABLED": "false",
            "JOB_MAX_ATTEMPTS": "3",
            "JOB_BACKOFF_SECONDS": "10",
        }
    )


@pytest.fixture
def raw_engine(settings):
    """An engine on an empty database (no migrations applied)."""
    engine = make_engine(settings.database_url)
    if engine.dialect.name != "sqlite":
        with engine.begin() as conn:
            for table in ("outbox", "jobs", "records", "schema_migrations"):
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    yield engine
    engine.dispose()


@pytest.fixture
def engine(raw_engine):
    apply_migrations(raw_engine)
    return raw_engine


@pytest.fixture
def notifier():
    return MockNotifier()


@pytest.fixture
def app(settings, engine, notifier):
    return create_app(settings, engine, notifier)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def worker(app) -> Worker:
    """The app's worker, driven by hand with run_once / drain."""
    return app.state.worker


@pytest.fixture
def webform_payload() -> dict:
    return {
        "submission_id": "wf-test-1",
        "submitted_at": "2026-11-10T09:00:00-05:00",
        "form": "quote",
        "answers": {
            "full_name": "  Alex   Sample ",
            "phone": "(202) 555-0101",
            "email": "Alex.Sample@Example.com",
            "message": "Please send a quote for two doors.",
        },
    }
