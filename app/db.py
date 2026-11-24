"""Engine factory and table definitions.

The tables are declared with SQLAlchemy Core so the same queries run on
SQLite (demo, tests) and Postgres (production). The schema itself is created
by the SQL files in ``migrations/``, not by ``metadata.create_all``; a test
checks that the two stay in step.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import DateTime, TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone aware datetimes, always UTC.

    Postgres stores TIMESTAMPTZ natively. SQLite has no timezone support, so
    values are converted to UTC before they are written and get the UTC
    marker back when they are read.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to a UTCDateTime column")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


JsonType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")

metadata = MetaData()

records = Table(
    "records",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("occurred_at", UTCDateTime, nullable=False),
    Column("received_at", UTCDateTime, nullable=False),
    Column("contact_name", Text),
    Column("contact_phone", Text),
    Column("contact_email", Text),
    Column("summary", Text, nullable=False, default=""),
    Column("raw", JsonType, nullable=False),
    Column("priority", Text, nullable=False, default="normal"),
    Column("tags", JsonType),
    Column("enriched_at", UTCDateTime),
    UniqueConstraint("source", "external_id", name="uq_records_source_external"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("payload", JsonType, nullable=False),
    Column("status", Text, nullable=False, default="queued"),
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=4),
    Column("run_after", UTCDateTime, nullable=False),
    Column("locked_at", UTCDateTime),
    Column("last_error", Text),
    Column("record_id", Text, ForeignKey("records.id")),
    Column("created_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", Text, primary_key=True),
    Column("channel", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("delivered_via", Text, nullable=False),
    Column("record_id", Text, ForeignKey("records.id")),
    Column("created_at", UTCDateTime, nullable=False),
)


def normalise_database_url(url: str) -> str:
    """Accept the common Postgres URL spellings and pick the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(database_url: str) -> Engine:
    url = normalise_database_url(database_url)
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, pool_pre_ping=True)

    database = parsed.database
    in_memory = database in (None, "", ":memory:")
    kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 15}}
    if in_memory:
        # One shared connection, otherwise every checkout sees an empty database.
        kwargs["poolclass"] = StaticPool
    else:
        Path(database).resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if not in_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine
