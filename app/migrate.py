"""Forward only SQL migration runner.

Migration files live in ``migrations/`` and are named ``NNN_description.sql``.
They are plain SQL with two placeholders so one file serves both databases:

* ``{{TIMESTAMP}}`` becomes ``TIMESTAMPTZ`` on Postgres and ``TIMESTAMP`` on SQLite
* ``{{JSON}}`` becomes ``JSONB`` on Postgres and ``TEXT`` on SQLite

Applied versions are tracked in ``schema_migrations`` together with a
checksum, so an edited migration that already ran is reported instead of
being silently ignored.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.db import utcnow

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_FILE_PATTERN = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.sql$")

_PLACEHOLDERS = {
    "postgresql": {"{{TIMESTAMP}}": "TIMESTAMPTZ", "{{JSON}}": "JSONB"},
    "sqlite": {"{{TIMESTAMP}}": "TIMESTAMP", "{{JSON}}": "TEXT"},
}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


def discover(directory: Path | None = None) -> list[Migration]:
    directory = MIGRATIONS_DIR if directory is None else Path(directory)
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_PATTERN.match(path.name)
        if not match:
            raise MigrationError(f"migration file name not understood: {path.name}")
        # Normalise line endings so a Windows checkout has the same checksum.
        sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        found.append(Migration(match.group(1), match.group(2), sql, checksum))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError("two migration files share a version number")
    return found


def render(sql: str, dialect: str) -> list[str]:
    """Fill the placeholders and split a file into single statements."""
    replacements = _PLACEHOLDERS.get(dialect)
    if replacements is None:
        raise MigrationError(f"unsupported database dialect: {dialect}")
    for placeholder, value in replacements.items():
        sql = sql.replace(placeholder, value)
    lines = [line for line in sql.split("\n") if not line.strip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def _ensure_tracking_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
        )


def applied_versions(engine: Engine) -> dict[str, str]:
    """Return {version: checksum} for migrations that already ran."""
    if not inspect(engine).has_table("schema_migrations"):
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version, checksum FROM schema_migrations")).all()
    return {row[0]: row[1] for row in rows}


def pending_migrations(engine: Engine, directory: Path | None = None) -> list[Migration]:
    applied = applied_versions(engine)
    return [m for m in discover(directory) if m.version not in applied]


def apply_migrations(engine: Engine, directory: Path | None = None) -> list[Migration]:
    """Apply every pending migration in order. Safe to run repeatedly."""
    migrations = discover(directory)
    _ensure_tracking_table(engine)
    applied = applied_versions(engine)

    for migration in migrations:
        known = applied.get(migration.version)
        if known is not None and known != migration.checksum:
            raise MigrationError(
                f"migration {migration.version}_{migration.name} was edited after it was applied; "
                "add a new migration instead"
            )

    newly_applied: list[Migration] = []
    dialect = engine.dialect.name
    for migration in migrations:
        if migration.version in applied:
            continue
        with engine.begin() as conn:
            for statement in render(migration.sql, dialect):
                conn.execute(text(statement))
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (:version, :name, :checksum, :applied_at)"
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                    "applied_at": utcnow().isoformat(),
                },
            )
        newly_applied.append(migration)
    return newly_applied
