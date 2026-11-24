"""Database backed background jobs.

There is no broker. Jobs are rows in the ``jobs`` table, written in the same
transaction as the data they belong to. A worker claims one job at a time
with a conditional UPDATE, which is meant to be safe with several workers on SQLite
and Postgres (the tests only ever run one worker at a time).

Delivery is at least once: a handler can run again after a crash or a
failure, so handlers must be safe to repeat.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.config import Settings
from app.db import jobs, outbox, records, utcnow
from app.enrich import classify
from app.notify import MockNotifier, Notifier

log = logging.getLogger("ops.jobs")

QUEUED, RUNNING, DONE, DEAD = "queued", "running", "done", "dead"
STATUSES = (QUEUED, RUNNING, DONE, DEAD)
ERROR_LIMIT = 500


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help. The job goes to dead."""


@dataclass
class JobContext:
    conn: Connection
    settings: Settings
    notifier: Notifier
    now: datetime
    job: Mapping[str, Any]


Handler = Callable[[JobContext, dict], None]


def enqueue(
    conn: Connection,
    kind: str,
    payload: dict,
    *,
    record_id: str | None = None,
    max_attempts: int = 4,
    run_after: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """Insert a job on the caller's connection, so it commits with their work."""
    now = now or utcnow()
    job_id = uuid.uuid4().hex
    conn.execute(
        insert(jobs).values(
            id=job_id,
            kind=kind,
            payload=payload,
            status=QUEUED,
            attempts=0,
            max_attempts=max(1, max_attempts),
            run_after=run_after or now,
            locked_at=None,
            last_error=None,
            record_id=record_id,
            created_at=now,
            finished_at=None,
        )
    )
    return job_id


def claim_next(engine: Engine, now: datetime) -> dict[str, Any] | None:
    """Move the oldest due job to running and return it, or None."""
    with engine.begin() as conn:
        candidates = conn.execute(
            select(jobs.c.id)
            .where(jobs.c.status == QUEUED, jobs.c.run_after <= now)
            .order_by(jobs.c.run_after, jobs.c.created_at, jobs.c.id)
            .limit(5)
        ).scalars().all()
        for job_id in candidates:
            claimed = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status == QUEUED)
                .values(status=RUNNING, attempts=jobs.c.attempts + 1, locked_at=now)
            )
            if claimed.rowcount == 1:
                row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
                return dict(row)
    return None


def requeue_stale(engine: Engine, now: datetime, visibility_timeout_seconds: float) -> int:
    """Return jobs to the queue whose worker died while holding them.

    A job that has already used its attempt budget goes to dead instead, so a
    job that kills its worker every time cannot loop forever. Returns how many
    jobs were put back on the queue.
    """
    cutoff = now - timedelta(seconds=visibility_timeout_seconds)
    stale = (jobs.c.status == RUNNING, jobs.c.locked_at < cutoff)
    with engine.begin() as conn:
        conn.execute(
            update(jobs)
            .where(*stale, jobs.c.attempts >= jobs.c.max_attempts)
            .values(
                status=DEAD,
                finished_at=now,
                locked_at=None,
                last_error="worker lost the job; attempts exhausted",
            )
        )
        result = conn.execute(
            update(jobs)
            .where(*stale)
            .values(status=QUEUED, locked_at=None, last_error="worker lost the job; requeued")
        )
    return result.rowcount or 0


def backoff_seconds(base: float, attempts: int) -> float:
    """Exponential backoff: base, 2x base, 4x base, capped at one hour."""
    return min(base * (2 ** max(0, attempts - 1)), 3600.0)


# ---------------------------------------------------------------- handlers


def handle_enrich_record(ctx: JobContext, payload: dict) -> None:
    record_id = payload.get("record_id")
    row = ctx.conn.execute(select(records).where(records.c.id == record_id)).mappings().first()
    if row is None:
        raise PermanentJobError(f"record {record_id} does not exist")
    priority, tags = classify(row["event_type"], row["summary"])
    first_time = row["enriched_at"] is None
    ctx.conn.execute(
        update(records)
        .where(records.c.id == record_id)
        .values(priority=priority, tags=tags, enriched_at=ctx.now)
    )
    # Only fan out on the first pass, so a repeated run does not notify twice.
    if priority == "high" and first_time:
        enqueue(
            ctx.conn,
            "notify",
            {"record_id": record_id},
            record_id=record_id,
            max_attempts=ctx.settings.job_max_attempts,
            now=ctx.now,
        )


def handle_notify(ctx: JobContext, payload: dict) -> None:
    record_id = payload.get("record_id")
    row = ctx.conn.execute(select(records).where(records.c.id == record_id)).mappings().first()
    if row is None:
        raise PermanentJobError(f"record {record_id} does not exist")
    who = row["contact_name"] or row["contact_phone"] or row["contact_email"] or "unknown contact"
    subject = f"High priority {row['event_type']} from {who}"
    body = f"[{row['source']}] {row['summary']}"
    ctx.notifier.send(subject, body)
    ctx.conn.execute(
        insert(outbox).values(
            id=uuid.uuid4().hex,
            channel="ops-alerts",
            subject=subject,
            body=body,
            delivered_via=ctx.notifier.name,
            record_id=record_id,
            created_at=ctx.now,
        )
    )


DEFAULT_HANDLERS: dict[str, Handler] = {
    "enrich_record": handle_enrich_record,
    "notify": handle_notify,
}


# ---------------------------------------------------------------- worker


class Worker:
    """Runs jobs one at a time. Use ``run_once`` in tests, ``start`` in the app."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        notifier: Notifier | None = None,
        handlers: Mapping[str, Handler] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.engine = engine
        self.settings = settings
        self.notifier = notifier or MockNotifier()
        self.handlers = dict(DEFAULT_HANDLERS if handlers is None else handlers)
        self.clock = clock
        self.heartbeat: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- single job ---------------------------------------------------

    def run_once(self) -> bool:
        """Claim and run one job. Returns False when nothing was due."""
        now = self.clock()
        job = claim_next(self.engine, now)
        if job is None:
            return False
        extra = {"job_id": job["id"], "kind": job["kind"], "attempt": job["attempts"]}
        handler = self.handlers.get(job["kind"])
        try:
            if handler is None:
                raise PermanentJobError(f"no handler registered for kind {job['kind']!r}")
            # Handler writes and the done mark commit together or not at all.
            with self.engine.begin() as conn:
                handler(JobContext(conn, self.settings, self.notifier, now, job), job["payload"])
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == job["id"])
                    .values(status=DONE, finished_at=now, locked_at=None, last_error=None)
                )
            log.info("job done", extra=extra)
        except PermanentJobError as exc:
            self._finish_failed(job, exc, now, retry=False)
            log.error("job dead", extra={**extra, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a job must never kill the worker
            retry = job["attempts"] < job["max_attempts"]
            self._finish_failed(job, exc, now, retry=retry)
            log.warning("job failed", extra={**extra, "error": str(exc), "will_retry": retry})
        return True

    def _finish_failed(self, job: Mapping[str, Any], exc: Exception, now: datetime, *, retry: bool) -> None:
        message = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
        if retry:
            delay = backoff_seconds(self.settings.job_backoff_seconds, job["attempts"])
            values = {
                "status": QUEUED,
                "run_after": now + timedelta(seconds=delay),
                "locked_at": None,
                "last_error": message,
            }
        else:
            values = {"status": DEAD, "finished_at": now, "locked_at": None, "last_error": message}
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(**values))

    def drain(self, limit: int = 1000) -> int:
        """Run due jobs until the queue is empty. Returns how many ran."""
        count = 0
        while count < limit and self.run_once():
            count += 1
        return count

    # -- background thread --------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.heartbeat = self.clock()
        self._thread = threading.Thread(target=self._loop, name="ops-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def run_forever(self) -> None:
        """Blocking loop for a standalone worker process."""
        self._stop.clear()
        self._loop()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        log.info("worker started", extra={"poll_seconds": self.settings.worker_poll_seconds})
        while not self._stop.is_set():
            self.heartbeat = self.clock()
            worked = False
            try:
                requeue_stale(self.engine, self.heartbeat, self.settings.job_visibility_timeout_seconds)
                worked = self.run_once()
            except Exception:  # noqa: BLE001 - keep polling through database hiccups
                log.exception("worker loop error")
            if not worked:
                self._stop.wait(self.settings.worker_poll_seconds)
        log.info("worker stopped")
