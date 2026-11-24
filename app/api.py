"""HTTP routes. ``public`` needs no key, ``secured`` needs one."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine

from app import SERVICE_NAME, __version__
from app.auth import require_api_key
from app.db import jobs, outbox, records, utcnow
from app.ingest import IngestResult, ingest_event
from app.jobs import DEAD, QUEUED, STATUSES
from app.migrate import pending_migrations
from app.normalizers import NormalizationError, UnknownSourceError, known_sources, parse_timestamp

public = APIRouter(tags=["health"])
secured = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

BATCH_LIMIT = 500


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def record_out(row: Mapping[str, Any], include_raw: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "source": row["source"],
        "external_id": row["external_id"],
        "event_type": row["event_type"],
        "occurred_at": _iso(row["occurred_at"]),
        "received_at": _iso(row["received_at"]),
        "contact": {
            "name": row["contact_name"],
            "phone": row["contact_phone"],
            "email": row["contact_email"],
        },
        "summary": row["summary"],
        "priority": row["priority"],
        "tags": row["tags"] or [],
        "enriched_at": _iso(row["enriched_at"]),
    }
    if include_raw:
        out["raw"] = row["raw"]
    return out


def job_out(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "run_after": _iso(row["run_after"]),
        "last_error": row["last_error"],
        "record_id": row["record_id"],
        "created_at": _iso(row["created_at"]),
        "finished_at": _iso(row["finished_at"]),
        "payload": row["payload"],
    }


def _engine(request: Request) -> Engine:
    return request.app.state.engine


# ---------------------------------------------------------------- health


@public.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Touches nothing else."""
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@public.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Readiness: database reachable, schema current, worker polling."""
    engine = _engine(request)
    settings = request.app.state.settings
    worker = request.app.state.worker
    checks: dict[str, dict[str, Any]] = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "dialect": engine.dialect.name}
    except Exception as exc:  # noqa: BLE001 - report, do not crash the probe
        checks["database"] = {"ok": False, "error": type(exc).__name__}

    if checks["database"]["ok"]:
        try:
            pending = [f"{m.version}_{m.name}" for m in pending_migrations(engine)]
            checks["migrations"] = {"ok": not pending, "pending": pending}
        except Exception as exc:  # noqa: BLE001
            checks["migrations"] = {"ok": False, "error": type(exc).__name__}
    else:
        checks["migrations"] = {"ok": False, "error": "database unavailable"}

    if settings.worker_enabled:
        beat = worker.heartbeat
        age = (utcnow() - beat).total_seconds() if beat else None
        fresh = age is not None and age < max(10.0, settings.worker_poll_seconds * 5)
        checks["worker"] = {"ok": worker.is_alive() and fresh, "enabled": True, "heartbeat_age_seconds": age}
    else:
        checks["worker"] = {"ok": True, "enabled": False}

    ready = all(check["ok"] for check in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "not_ready", "checks": checks}


# ---------------------------------------------------------------- ingest


def _ingest(request: Request, source: str, payload: Any) -> IngestResult:
    return ingest_event(
        _engine(request),
        source,
        payload,
        max_attempts=request.app.state.settings.job_max_attempts,
    )


def _require_source(source: str) -> None:
    if source not in known_sources():
        raise HTTPException(404, detail=f"unknown source {source!r}; known sources: {known_sources()}")


@secured.get("/sources", tags=["ingest"])
def list_sources() -> dict[str, list[str]]:
    return {"sources": known_sources()}


@secured.post("/ingest/{source}", tags=["ingest"])
def ingest_one(
    source: str,
    request: Request,
    response: Response,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Accept one event in the source's own shape. Replays return 200, new events 201."""
    _require_source(source)
    try:
        result = _ingest(request, source, payload)
    except UnknownSourceError:
        raise HTTPException(404, detail=f"unknown source {source!r}") from None
    except NormalizationError as exc:
        raise HTTPException(422, detail={"message": "payload rejected", "problems": exc.problems}) from None
    response.status_code = 201 if result.created else 200
    return {
        "status": "created" if result.created else "duplicate",
        "record": record_out(result.record),
        "job_id": result.job_id,
    }


@secured.post("/ingest/{source}/batch", tags=["ingest"])
def ingest_batch(
    source: str,
    request: Request,
    events: list[dict[str, Any]] = Body(..., embed=True),
) -> dict[str, Any]:
    """Accept many events. Each is judged alone, one bad event does not sink the rest."""
    _require_source(source)
    if len(events) > BATCH_LIMIT:
        raise HTTPException(413, detail=f"at most {BATCH_LIMIT} events per batch")
    results: list[dict[str, Any]] = []
    counts = {"created": 0, "duplicate": 0, "rejected": 0}
    for index, payload in enumerate(events):
        try:
            result = _ingest(request, source, payload)
        except NormalizationError as exc:
            counts["rejected"] += 1
            results.append({"index": index, "status": "rejected", "problems": exc.problems})
            continue
        status = "created" if result.created else "duplicate"
        counts[status] += 1
        results.append({"index": index, "status": status, "record_id": result.record["id"]})
    return {"counts": counts, "results": results}


# ---------------------------------------------------------------- records


@secured.get("/records", tags=["records"])
def list_records(
    request: Request,
    source: str | None = None,
    event_type: str | None = None,
    priority: str | None = None,
    since: str | None = Query(None, description="Only events at or after this time (ISO 8601)"),
    q: str | None = Query(None, description="Case insensitive match on the summary"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    conditions = []
    if source:
        conditions.append(records.c.source == source)
    if event_type:
        conditions.append(records.c.event_type == event_type)
    if priority:
        conditions.append(records.c.priority == priority)
    if since:
        try:
            conditions.append(records.c.occurred_at >= parse_timestamp(since))
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
    if q:
        # autoescape: "%" and "_" in the search text are literal, not wildcards.
        conditions.append(records.c.summary.icontains(q, autoescape=True))
    with _engine(request).connect() as conn:
        total = conn.execute(select(func.count()).select_from(records).where(*conditions)).scalar_one()
        rows = conn.execute(
            select(records)
            .where(*conditions)
            .order_by(records.c.occurred_at.desc(), records.c.id)
            .limit(limit)
            .offset(offset)
        ).mappings().all()
    return {"total": total, "limit": limit, "offset": offset, "items": [record_out(row) for row in rows]}


@secured.get("/records/{record_id}", tags=["records"])
def get_record(record_id: str, request: Request) -> dict[str, Any]:
    with _engine(request).connect() as conn:
        row = conn.execute(select(records).where(records.c.id == record_id)).mappings().first()
        if row is None:
            raise HTTPException(404, detail="record not found")
        job_rows = conn.execute(
            select(jobs).where(jobs.c.record_id == record_id).order_by(jobs.c.created_at, jobs.c.id)
        ).mappings().all()
    out = record_out(row, include_raw=True)
    out["jobs"] = [job_out(job) for job in job_rows]
    return out


# ---------------------------------------------------------------- jobs


@secured.get("/jobs", tags=["jobs"])
def list_jobs(
    request: Request,
    status: str | None = None,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if status and status not in STATUSES:
        raise HTTPException(422, detail=f"status must be one of {list(STATUSES)}")
    conditions = []
    if status:
        conditions.append(jobs.c.status == status)
    if kind:
        conditions.append(jobs.c.kind == kind)
    with _engine(request).connect() as conn:
        total = conn.execute(select(func.count()).select_from(jobs).where(*conditions)).scalar_one()
        rows = conn.execute(
            select(jobs)
            .where(*conditions)
            .order_by(jobs.c.created_at.desc(), jobs.c.id)
            .limit(limit)
            .offset(offset)
        ).mappings().all()
    return {"total": total, "limit": limit, "offset": offset, "items": [job_out(row) for row in rows]}


@secured.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    with _engine(request).connect() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
    if row is None:
        raise HTTPException(404, detail="job not found")
    return job_out(row)


@secured.post("/jobs/{job_id}/retry", tags=["jobs"])
def retry_job(job_id: str, request: Request) -> dict[str, Any]:
    """Put a dead job back on the queue with a fresh attempt budget."""
    with _engine(request).begin() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
        if row is None:
            raise HTTPException(404, detail="job not found")
        changed = conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id, jobs.c.status == DEAD)
            .values(status=QUEUED, attempts=0, run_after=utcnow(), finished_at=None, locked_at=None)
        )
        if changed.rowcount != 1:
            raise HTTPException(409, detail=f"only dead jobs can be retried; this one is {row['status']}")
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
    return job_out(row)


# ---------------------------------------------------------------- stats


def _grouped(conn, column) -> dict[str, int]:
    rows = conn.execute(select(column, func.count()).group_by(column).order_by(column)).all()
    return {key: count for key, count in rows}


@secured.get("/stats", tags=["stats"])
def stats(request: Request) -> dict[str, Any]:
    with _engine(request).connect() as conn:
        return {
            "records": {
                "total": conn.execute(select(func.count()).select_from(records)).scalar_one(),
                "by_source": _grouped(conn, records.c.source),
                "by_event_type": _grouped(conn, records.c.event_type),
                "by_priority": _grouped(conn, records.c.priority),
            },
            "jobs": {"by_status": _grouped(conn, jobs.c.status), "by_kind": _grouped(conn, jobs.c.kind)},
            "outbox": {"total": conn.execute(select(func.count()).select_from(outbox)).scalar_one()},
        }


@secured.get("/outbox", tags=["stats"])
def list_outbox(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    with _engine(request).connect() as conn:
        rows = conn.execute(
            select(outbox).order_by(outbox.c.created_at.desc(), outbox.c.id).limit(limit)
        ).mappings().all()
    return {
        "items": [
            {
                "id": row["id"],
                "channel": row["channel"],
                "subject": row["subject"],
                "body": row["body"],
                "delivered_via": row["delivered_via"],
                "record_id": row["record_id"],
                "created_at": _iso(row["created_at"]),
            }
            for row in rows
        ]
    }
