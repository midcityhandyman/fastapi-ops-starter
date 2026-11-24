"""Write normalised events to the record table, exactly once per event."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.db import records, utcnow
from app.jobs import enqueue
from app.normalizers import NormalizedEvent, normalize


@dataclass(frozen=True)
class IngestResult:
    record: dict[str, Any]
    created: bool
    job_id: str | None


def _find(conn: Connection, source: str, external_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        select(records).where(records.c.source == source, records.c.external_id == external_id)
    ).mappings().first()
    return dict(row) if row else None


def _insert(conn: Connection, event: NormalizedEvent, payload: dict, now: datetime) -> dict[str, Any]:
    values = {
        "id": uuid.uuid4().hex,
        "source": event.source,
        "external_id": event.external_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "received_at": now,
        "contact_name": event.contact_name,
        "contact_phone": event.contact_phone,
        "contact_email": event.contact_email,
        "summary": event.summary,
        "raw": payload,
        "priority": "normal",
        "tags": None,
        "enriched_at": None,
    }
    conn.execute(insert(records).values(**values))
    return values


def ingest_event(
    engine: Engine,
    source: str,
    payload: Any,
    *,
    max_attempts: int = 4,
    now: datetime | None = None,
) -> IngestResult:
    """Normalise and store one event.

    The record and its follow up job are written in one transaction, so a
    record never exists without its enrichment job. A replay of the same
    (source, external_id) returns the stored record with ``created=False``.

    Raises UnknownSourceError or NormalizationError for bad input.
    """
    event = normalize(source, payload)
    now = now or utcnow()
    try:
        with engine.begin() as conn:
            existing = _find(conn, event.source, event.external_id)
            if existing is not None:
                return IngestResult(existing, False, None)
            record = _insert(conn, event, payload, now)
            job_id = enqueue(
                conn,
                "enrich_record",
                {"record_id": record["id"]},
                record_id=record["id"],
                max_attempts=max_attempts,
                now=now,
            )
            return IngestResult(record, True, job_id)
    except IntegrityError:
        # Two deliveries of the same event raced; the other one won.
        with engine.connect() as conn:
            existing = _find(conn, event.source, event.external_id)
        if existing is None:
            raise
        return IngestResult(existing, False, None)
