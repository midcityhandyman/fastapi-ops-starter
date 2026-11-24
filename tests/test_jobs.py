import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from app.db import jobs, outbox
from app.ingest import ingest_event
from app.jobs import DEFAULT_HANDLERS, PermanentJobError, Worker, backoff_seconds, claim_next, enqueue, requeue_stale
from app.notify import MockNotifier, WebhookNotifier, build_notifier
from tests.conftest import AUTH

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def _job(engine, job_id):
    with engine.connect() as conn:
        return dict(conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one())


def _urgent_sms(sid="j1"):
    return {"message_sid": sid, "from": "+12025550113", "body": "urgent: pipe is leaking", "timestamp": 1793628000}


# ---------------------------------------------------------------- happy path


def test_enrich_then_notify(client, worker, notifier, engine):
    record_id = client.post("/v1/ingest/sms", json=_urgent_sms(), headers=AUTH).json()["record"]["id"]

    assert worker.drain() == 2  # enrich_record, then the notify it fans out
    assert worker.run_once() is False

    record = client.get(f"/v1/records/{record_id}", headers=AUTH).json()
    assert record["priority"] == "high"
    assert record["tags"] == ["urgent"]
    assert record["enriched_at"] is not None
    assert [(job["kind"], job["status"]) for job in record["jobs"]] == [
        ("enrich_record", "done"),
        ("notify", "done"),
    ]

    assert len(notifier.sent) == 1
    assert notifier.sent[0]["subject"] == "High priority message_received from +12025550113"
    box = client.get("/v1/outbox", headers=AUTH).json()["items"]
    assert len(box) == 1 and box[0]["delivered_via"] == "mock" and box[0]["record_id"] == record_id


def test_normal_priority_does_not_notify(client, worker, notifier):
    client.post(
        "/v1/ingest/sms",
        json={"message_sid": "calm", "from": "+12025550111", "body": "see you then", "timestamp": 5},
        headers=AUTH,
    )
    assert worker.drain() == 1
    assert notifier.sent == []


def test_rerunning_enrichment_does_not_notify_twice(client, worker, notifier, engine):
    record_id = client.post("/v1/ingest/sms", json=_urgent_sms(), headers=AUTH).json()["record"]["id"]
    worker.drain()
    with engine.begin() as conn:
        enqueue(conn, "enrich_record", {"record_id": record_id}, record_id=record_id)
    assert worker.drain() == 1
    assert len(notifier.sent) == 1


# ---------------------------------------------------------------- failure


def test_retry_with_backoff_then_dead(engine, settings):
    clock = FakeClock()
    calls = []

    def flaky(ctx, payload):
        calls.append(ctx.now)
        raise RuntimeError("downstream is down")

    worker = Worker(engine, settings, MockNotifier(), handlers={"flaky": flaky}, clock=clock)
    with engine.begin() as conn:
        job_id = enqueue(conn, "flaky", {}, max_attempts=3, now=clock.now)

    assert worker.run_once() is True
    job = _job(engine, job_id)
    assert (job["status"], job["attempts"]) == ("queued", 1)
    assert job["last_error"] == "RuntimeError: downstream is down"
    assert job["run_after"] == T0 + timedelta(seconds=10)

    assert worker.run_once() is False  # not due yet
    clock.advance(10)
    assert worker.run_once() is True
    assert _job(engine, job_id)["run_after"] == T0 + timedelta(seconds=30)  # 10 + 20

    clock.advance(20)
    assert worker.run_once() is True
    job = _job(engine, job_id)
    assert (job["status"], job["attempts"]) == ("dead", 3)
    assert job["finished_at"] == clock.now
    assert len(calls) == 3
    assert worker.run_once() is False


def test_failed_handler_writes_are_rolled_back(engine, settings):
    def half_done(ctx, payload):
        enqueue(ctx.conn, "never", {})
        raise RuntimeError("after the write")

    worker = Worker(engine, settings, handlers={"half_done": half_done}, clock=FakeClock())
    with engine.begin() as conn:
        enqueue(conn, "half_done", {}, now=T0)
    worker.run_once()
    with engine.connect() as conn:
        kinds = conn.execute(select(jobs.c.kind)).scalars().all()
    assert kinds == ["half_done"]


def test_unknown_kind_and_permanent_error_go_straight_to_dead(engine, settings):
    def hopeless(ctx, payload):
        raise PermanentJobError("bad input")

    worker = Worker(engine, settings, handlers={"hopeless": hopeless}, clock=FakeClock())
    with engine.begin() as conn:
        unknown = enqueue(conn, "mystery", {}, now=T0)
        permanent = enqueue(conn, "hopeless", {}, now=T0)
    assert worker.drain() == 2
    assert _job(engine, unknown)["status"] == "dead"
    assert "no handler" in _job(engine, unknown)["last_error"]
    job = _job(engine, permanent)
    assert (job["status"], job["attempts"]) == ("dead", 1)


def test_enrich_for_missing_record_is_dead(engine, settings):
    worker = Worker(engine, settings, clock=FakeClock())
    with engine.begin() as conn:
        job_id = enqueue(conn, "enrich_record", {"record_id": "gone"}, now=T0)
    worker.run_once()
    assert _job(engine, job_id)["status"] == "dead"


def test_notifier_failure_is_retried_and_leaves_no_outbox_row(engine, settings):
    class Broken:
        name = "broken"

        def send(self, subject, body):
            raise ConnectionError("webhook unreachable")

    clock = FakeClock()
    result = ingest_event(engine, "sms", _urgent_sms(), now=T0)
    worker = Worker(engine, settings, Broken(), clock=clock)
    assert worker.drain() == 2
    with engine.connect() as conn:
        notify = dict(conn.execute(select(jobs).where(jobs.c.kind == "notify")).mappings().one())
        assert conn.execute(select(outbox)).all() == []
    assert notify["status"] == "queued" and notify["record_id"] == result.record["id"]
    assert "webhook unreachable" in notify["last_error"]


# ---------------------------------------------------------------- queue mechanics


def test_claim_order_and_single_claim(engine):
    with engine.begin() as conn:
        later = enqueue(conn, "a", {}, now=T0, run_after=T0 + timedelta(seconds=5))
        sooner = enqueue(conn, "a", {}, now=T0)
    first = claim_next(engine, T0 + timedelta(seconds=10))
    second = claim_next(engine, T0 + timedelta(seconds=10))
    assert (first["id"], second["id"]) == (sooner, later)
    assert first["status"] == "running" and first["attempts"] == 1
    assert claim_next(engine, T0 + timedelta(seconds=10)) is None


def test_stale_running_jobs_are_requeued(engine):
    with engine.begin() as conn:
        job_id = enqueue(conn, "a", {}, now=T0)
    claim_next(engine, T0)
    assert requeue_stale(engine, T0 + timedelta(seconds=60), 300) == 0
    assert requeue_stale(engine, T0 + timedelta(seconds=301), 300) == 1
    job = _job(engine, job_id)
    assert job["status"] == "queued" and job["locked_at"] is None


def test_stale_job_out_of_attempts_goes_dead_instead_of_looping(engine):
    with engine.begin() as conn:
        job_id = enqueue(conn, "a", {}, now=T0, max_attempts=2)
    now = T0
    for _ in range(2):  # the worker dies while holding the job, twice
        assert claim_next(engine, now)["id"] == job_id
        now += timedelta(seconds=301)
        requeue_stale(engine, now, 300)
    job = _job(engine, job_id)
    assert job["status"] == "dead" and job["attempts"] == 2
    assert job["finished_at"] == now and "attempts exhausted" in job["last_error"]
    assert claim_next(engine, now) is None


def test_backoff_is_exponential_and_capped():
    assert [backoff_seconds(5, n) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert backoff_seconds(5, 40) == 3600


def test_retry_endpoint(client, engine, worker):
    with engine.begin() as conn:
        job_id = enqueue(conn, "mystery", {})
    assert client.post(f"/v1/jobs/{job_id}/retry", headers=AUTH).status_code == 409  # still queued
    worker.drain()
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()["status"] == "dead"
    assert client.get("/v1/jobs?status=dead", headers=AUTH).json()["total"] == 1

    retried = client.post(f"/v1/jobs/{job_id}/retry", headers=AUTH)
    assert retried.status_code == 200
    assert (retried.json()["status"], retried.json()["attempts"]) == ("queued", 0)

    assert client.post("/v1/jobs/nope/retry", headers=AUTH).status_code == 404
    assert client.get("/v1/jobs/nope", headers=AUTH).status_code == 404
    assert client.get("/v1/jobs?status=sideways", headers=AUTH).status_code == 422


# ---------------------------------------------------------------- background thread


def test_background_thread_drains_queue_and_reports_ready(settings, engine):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.main import create_app

    live = replace(settings, worker_enabled=True, worker_poll_seconds=0.05)
    notifier = MockNotifier()
    with TestClient(create_app(live, engine, notifier)) as client:
        client.post("/v1/ingest/sms", json=_urgent_sms("bg-1"), headers=AUTH)
        deadline = time.time() + 10
        while time.time() < deadline:
            by_status = client.get("/v1/stats", headers=AUTH).json()["jobs"]["by_status"]
            if by_status == {"done": 2}:
                break
            time.sleep(0.05)
        assert by_status == {"done": 2}
        ready = client.get("/readyz").json()
        assert ready["checks"]["worker"]["ok"] is True and ready["checks"]["worker"]["enabled"] is True
        assert any(thread.name == "ops-worker" for thread in threading.enumerate())
    assert not any(thread.name == "ops-worker" and thread.is_alive() for thread in threading.enumerate())
    assert len(notifier.sent) == 1


# ---------------------------------------------------------------- notifiers


def test_webhook_notifier_posts_json_with_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = request.read().decode()
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    WebhookNotifier("https://hooks.example.com/ops", "fake-token", client).send("Subject", "Body")
    assert seen["auth"] == "Bearer fake-token"
    assert '"subject":"Subject"' in seen["json"].replace(" ", "")


def test_webhook_notifier_raises_on_error_status():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        WebhookNotifier("https://hooks.example.com/ops", client=client).send("s", "b")


def test_build_notifier_defaults_to_mock(settings):
    from dataclasses import replace

    assert isinstance(build_notifier(settings), MockNotifier)
    hooked = build_notifier(replace(settings, notify_webhook_url="https://hooks.example.com/ops"))
    assert isinstance(hooked, WebhookNotifier)


def test_default_handlers_are_registered():
    assert set(DEFAULT_HANDLERS) == {"enrich_record", "notify"}
