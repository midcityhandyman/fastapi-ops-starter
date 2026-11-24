"""Offline end to end demo.

Runs the real application in process against a throwaway SQLite file:
migrate, probe health, show auth, ingest the seed events from four sources,
replay one, reject one, drain the background jobs, then read it all back.
No network, no keys, nothing to install beyond requirements.txt.

Usage: python demo/run_demo.py            (prints to the terminal)
       python demo/run_demo.py --save     (also writes demo/output.txt)
"""

from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import warnings
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.db import make_engine  # noqa: E402
from app.jobs import enqueue  # noqa: E402
from app.logging_setup import JsonFormatter  # noqa: E402
from app.main import create_app  # noqa: E402
from app.migrate import apply_migrations  # noqa: E402
from app.notify import MockNotifier  # noqa: E402
from app.seed import load_seed_events  # noqa: E402

DEMO_KEY = "demo-key-not-a-secret"
AUTH = {"X-API-Key": DEMO_KEY}


def heading(title: str) -> None:
    print(f"\n== {title} ==")


def run(workdir: Path) -> None:
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    settings = load_settings(
        {
            "APP_ENV": "demo",
            "DATABASE_URL": f"sqlite:///{(workdir / 'demo.db').as_posix()}",
            "OPS_API_KEYS": f"demo-client:{DEMO_KEY}",
            # The demo drives the worker by hand so the output is in a fixed order.
            "WORKER_ENABLED": "false",
        }
    )
    engine = make_engine(settings.database_url)
    notifier = MockNotifier()
    app = create_app(settings, engine, notifier)

    print("fastapi-ops-starter demo (offline, SQLite, synthetic data)")

    with TestClient(app) as client:
        heading("1. Readiness before and after the migration script")
        before = client.get("/readyz")
        print(f"GET /readyz -> {before.status_code} {before.json()['status']}, "
              f"pending: {before.json()['checks']['migrations']['pending']}")
        for migration in apply_migrations(engine):
            print(f"  applied {migration.version}_{migration.name}")
        after = client.get("/readyz")
        print(f"GET /readyz -> {after.status_code} {after.json()['status']}")
        print(f"GET /healthz -> {client.get('/healthz').status_code} {client.get('/healthz').json()['status']}")

        heading("2. API key auth")
        print(f"GET /v1/records with no key      -> {client.get('/v1/records').status_code}")
        wrong = client.get("/v1/records", headers={"X-API-Key": "definitely-wrong"})
        print(f"GET /v1/records with a wrong key -> {wrong.status_code}")
        print(f"GET /v1/records with the key     -> {client.get('/v1/records', headers=AUTH).status_code}")

        heading("3. Ingest 24 seed events from four sources")
        events = load_seed_events()
        outcomes: Counter = Counter()
        for entry in events:
            response = client.post(f"/v1/ingest/{entry['source']}", json=entry["payload"], headers=AUTH)
            outcomes[(entry["source"], response.status_code, response.json()["status"])] += 1
        for (source, code, status), count in sorted(outcomes.items()):
            print(f"  POST /v1/ingest/{source:<8} x{count} -> {code} {status}")

        heading("4. Replays and bad payloads")
        first = events[0]
        replay = client.post(f"/v1/ingest/{first['source']}", json=first["payload"], headers=AUTH)
        print(f"same webform event again -> {replay.status_code} {replay.json()['status']} (no new row)")
        bad = client.post("/v1/ingest/sms", json={"body": "no id, no sender"}, headers=AUTH)
        print(f"sms without required fields -> {bad.status_code}")
        for problem in bad.json()["detail"]["problems"]:
            print(f"  problem: {problem}")
        print(f"unknown source 'fax' -> {client.post('/v1/ingest/fax', json={}, headers=AUTH).status_code}")

        heading("5. Background jobs")
        with engine.begin() as conn:
            enqueue(conn, "send_fax", {"note": "no handler exists for this kind"})
        print(f"queue before: {client.get('/v1/stats', headers=AUTH).json()['jobs']['by_status']}")
        ran = app.state.worker.drain()
        stats = client.get("/v1/stats", headers=AUTH).json()
        print(f"worker ran {ran} jobs")
        print(f"queue after:  {stats['jobs']['by_status']}")
        print(f"by kind:      {stats['jobs']['by_kind']}")
        dead = client.get("/v1/jobs?status=dead", headers=AUTH).json()["items"][0]
        print(f"dead job: kind={dead['kind']} attempts={dead['attempts']} error={dead['last_error']}")

        heading("6. One record table, whatever the source")
        rows = client.get("/v1/records?limit=200", headers=AUTH).json()["items"]
        print(f"{'source':<8} {'event_type':<17} {'pri':<6} {'contact':<46} tags")
        for row in sorted(rows, key=lambda r: r["occurred_at"]):
            contact = row["contact"]
            who = " / ".join(
                part for part in (contact["name"], contact["phone"] or contact["email"]) if part
            ) or "(unknown)"
            print(f"{row['source']:<8} {row['event_type']:<17} {row['priority']:<6} {who:<46} {','.join(row['tags'])}")

        heading("7. One record in full")
        urgent = client.get("/v1/records?q=leaking&source=webform", headers=AUTH).json()["items"][0]
        detail = client.get(f"/v1/records/{urgent['id']}", headers=AUTH).json()
        shown = {key: detail[key] for key in ("source", "external_id", "event_type", "occurred_at", "contact", "summary", "priority", "tags")}
        shown["jobs"] = [f"{job['kind']}: {job['status']}" for job in detail["jobs"]]
        print(json.dumps(shown, indent=2))

        heading("8. Stats and the notification outbox (mock notifier, nothing sent)")
        print(f"records by source:   {stats['records']['by_source']}")
        print(f"records by priority: {stats['records']['by_priority']}")
        outbox_items = client.get("/v1/outbox", headers=AUTH).json()["items"]
        print(f"outbox rows: {len(outbox_items)} (mock notifier holds {len(notifier.sent)})")
        for item in sorted(outbox_items, key=lambda i: i["subject"]):
            print(f"  [{item['delivered_via']}] {item['subject']}")

    heading("9. Structured log lines (sample, ids and timings vary per run)")
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    samples = [
        next(line for line in lines if line["logger"] == "ops.access" and line.get("status") == 401),
        next(line for line in lines if line["logger"] == "ops.access" and line.get("status") == 201),
        next(line for line in lines if line["logger"] == "ops.jobs" and line["msg"] == "job done"),
        next(line for line in lines if line["logger"] == "ops.jobs" and line["msg"] == "job dead"),
    ]
    for line in samples:
        for volatile in ("ts", "request_id", "duration_ms", "job_id"):
            if volatile in line:
                line[volatile] = "<varies>"
        print(json.dumps(line))
    print(f"({len(lines)} log lines in total)")

    logging.getLogger().removeHandler(handler)
    engine.dispose()
    print("\nDemo finished.")


def main() -> int:
    save = "--save" in sys.argv[1:]
    buffer = io.StringIO()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        if save:
            with redirect_stdout(buffer):
                run(Path(tmp))
        else:
            run(Path(tmp))
    if save:
        text = buffer.getvalue()
        (ROOT / "demo" / "output.txt").write_text(text, encoding="utf-8", newline="\n")
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
