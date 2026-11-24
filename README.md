# fastapi-ops-starter

[![ci](https://github.com/midcityhandyman/fastapi-ops-starter/actions/workflows/ci.yml/badge.svg)](https://github.com/midcityhandyman/fastapi-ops-starter/actions/workflows/ci.yml)

**One place to see every inbound contact.** A small service business hears from customers through a web form, a text line, a phone system and an inbox. Each one sends a different shape, so enquiries get missed, counted twice or answered late.

This is the small backend that sits in front of all four, for a developer starting the operations side of a business like that:

- Every channel posts to one endpoint, and every event becomes one row with the same fields: who, when, what kind, a summary, and the original payload.
- A webhook that is delivered twice is stored once.
- Urgent messages, complaints and missed calls are marked high priority and raise an alert. That work runs as a background job that retries when it fails and is parked where you can see it when it cannot succeed.
- The records, the job queue, the failures and the alerts are all readable through the same API.

Built with FastAPI, SQLAlchemy Core and plain SQL migrations. It runs on SQLite with no setup and on Postgres. It is a starter, not a finished product: the four payload shapes are invented, and alerts go to a mock unless you configure a webhook. [Limitations](#limitations) says exactly what is and is not proven.

## What you will see

`python demo/run_demo.py` runs the real app offline against a throwaway SQLite file. These lines are copied from its captured output, [`demo/output.txt`](demo/output.txt). CI runs the demo on every push and fails if the output differs from that file.

```text
== 3. Ingest 24 seed events from four sources ==
  POST /v1/ingest/call     x6 -> 201 created
  POST /v1/ingest/email    x6 -> 201 created
  POST /v1/ingest/sms      x6 -> 201 created
  POST /v1/ingest/webform  x6 -> 201 created

== 4. Replays and bad payloads ==
same webform event again -> 200 duplicate (no new row)
sms without required fields -> 422
  problem: message_sid: Field required

== 5. Background jobs ==
queue before: {'queued': 25}
worker ran 33 jobs
queue after:  {'dead': 1, 'done': 32}
by kind:      {'enrich_record': 24, 'notify': 8, 'send_fax': 1}

source   event_type        pri    contact                                        tags
webform  form_submission   normal Alex Sample / +12025550101                     quote
webform  form_submission   high   Jordan Example / +12025550102                  urgent
call     call_missed       high   +12025550122                                   callback
```

The one `dead` job is deliberate: the demo queues a `send_fax` job that has no handler, to show where a job that cannot succeed ends up. All names, numbers and addresses are synthetic.

## What it does

- **API key auth.** Every `/v1` route needs a key (`X-API-Key` or `Authorization: Bearer`). Keys are held as SHA-256 digests and compared in constant time, and the log shows the key's name, never the key.
- **Ingestion that normalises.** `POST /v1/ingest/{source}` accepts the source's own payload (`webform`, `sms`, `call`, `email`) and writes one row to `records`: contact name, phone and email, time in UTC, event type, a summary, and the untouched original in `raw`.
- **Idempotent ingest.** `(source, external_id)` is unique in the database. A replayed webhook returns `200 duplicate` and creates nothing.
- **Background jobs without a broker.** Jobs are rows in the same database, written in the same transaction as the record. A worker thread claims them, retries with exponential backoff, and moves jobs that are out of attempts to `dead`, where you can read the error and retry them through the API.
- **Health and readiness.** `/healthz` says the process is up. `/readyz` checks the database, pending migrations and the in process worker's heartbeat, and returns 503 with the reason when any of them fail.
- **Structured logging.** One JSON object per line, with a request id that is taken from `X-Request-ID` or generated, and echoed back on the response.
- **A migration script.** Plain SQL files, applied in order, tracked with checksums. The same files run on SQLite and Postgres.
- **Seeded fake events.** 24 synthetic events across the four sources, loaded through the real ingestion path.

## Architecture

```mermaid
flowchart LR
    subgraph Sources
        W[Web form]
        S[SMS gateway]
        C[Phone system]
        E[Inbox]
    end

    W & S & C & E -->|"POST /v1/ingest/{source} + API key"| AUTH[API key check]

    subgraph API["FastAPI app"]
        AUTH --> NORM[Normalisers<br/>one per source]
        NORM --> TX[(One transaction:<br/>record + enrich job)]
        READ["GET /v1/records, /jobs, /stats, /outbox"]
        HEALTH["/healthz and /readyz"]
    end

    TX --> DB[(records / jobs / outbox<br/>SQLite or Postgres)]
    READ --> DB
    HEALTH --> DB

    subgraph Worker["Background worker"]
        CLAIM[Claim due job] --> ENRICH[enrich_record:<br/>priority and tags]
        ENRICH -->|high priority| NOTIFY[notify]
    end

    DB --> CLAIM
    ENRICH --> DB
    NOTIFY --> OUT{Notifier}
    OUT -->|default| MOCK[Mock: outbox row only]
    OUT -->|NOTIFY_WEBHOOK_URL set| HOOK[Webhook POST]

    MIG[scripts/migrate.py] -->|SQL files, checksummed| DB
    API -. JSON logs with request id .-> LOGS[stdout]
    Worker -. JSON logs .-> LOGS
```

## Run it

Python 3.12 and nothing else. No `.env` is needed for a local run.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/migrate.py --seed
python -m app serve
```

The server listens on `http://127.0.0.1:8000`. With no `OPS_API_KEYS` set and `APP_ENV` not `production`, it accepts the development key `dev-local-key` and says so in the log. In another terminal:

```bash
curl http://127.0.0.1:8000/readyz
curl -H "X-API-Key: dev-local-key" "http://127.0.0.1:8000/v1/records?priority=high"
curl -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{"message_sid":"SMfake9001","from":"+12025550199","body":"urgent, the tap is leaking","timestamp":1793628000}' \
  http://127.0.0.1:8000/v1/ingest/sms
```

The first returns `"status":"ready"`, the second the 8 high priority seed records, the third `"status":"created"` (and `"duplicate"` if you send it again). About a second later `GET /v1/stats` shows one more high priority record and one more outbox row. Interactive API docs are at `/docs`.

### Endpoints

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `GET /healthz` | none | Liveness |
| `GET /readyz` | none | Readiness: database, migrations, worker |
| `GET /v1/sources` | key | Sources the API understands |
| `POST /v1/ingest/{source}` | key | Ingest one event (201 created, 200 duplicate, 422 rejected) |
| `POST /v1/ingest/{source}/batch` | key | Ingest up to 500 events, each judged alone |
| `GET /v1/records` | key | List with `source`, `event_type`, `priority`, `since`, `q`, `limit`, `offset` |
| `GET /v1/records/{id}` | key | One record with its raw payload and jobs |
| `GET /v1/jobs`, `GET /v1/jobs/{id}` | key | Inspect the queue |
| `POST /v1/jobs/{id}/retry` | key | Put a dead job back on the queue |
| `GET /v1/stats`, `GET /v1/outbox` | key | Counts, and notifications sent |

### Configuration

Copy `.env.example` to `.env` and edit. The entry points read `.env` themselves; real environment variables win. The settings that matter most:

- `OPS_API_KEYS`: `name:key` pairs, or better `name:sha256:<digest>` as printed by `python -m app genkey --name crm`, so the raw key never sits on the server.
- `DATABASE_URL`: SQLite by default. For Postgres, `pip install -r requirements-postgres.txt` and set `DATABASE_URL=postgresql://user:password@host:5432/dbname`, then run `python scripts/migrate.py`.
- `WORKER_ENABLED=false` plus `python -m app worker` if you want the worker in its own process.
- `NOTIFY_WEBHOOK_URL`: leave empty and alerts go to the mock notifier (an `outbox` row, nothing leaves the machine).

### Migrations

```bash
python scripts/migrate.py --status   # what is applied, what is pending
python scripts/migrate.py            # apply pending
```

Add a migration by creating the next numbered file in `migrations/`. Use `{{TIMESTAMP}}` and `{{JSON}}` for the two types that differ between databases. Migrations are forward only, and a file that was edited after it ran is refused rather than silently skipped.

### Adding a source

In `app/normalizers.py`: add a pydantic model for what the source sends, a function that maps it onto `NormalizedEvent`, and one entry in `NORMALIZERS`. The route, the idempotency and the jobs come for free.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

86 tests, no network and no keys; the run takes a few seconds. They cover auth on every `/v1` route, each normaliser, idempotent ingest, batch ingest, record filters, job retry and backoff with a fake clock, rollback of a failed handler's writes, stale job recovery, the live worker thread, the migration runner (ordering, checksums, schema matching the table definitions), readiness, config parsing, log format, the seed data, and that the excerpt at the top of this file matches the captured demo output.

To run the same suite against Postgres, point `TEST_DATABASE_URL` at an empty database (the tests drop and recreate their tables):

```bash
pip install -r requirements-postgres.txt
TEST_DATABASE_URL=postgresql://ops:password@localhost:5432/ops_test python -m pytest
```

CI (`.github/workflows/ci.yml`) runs the suite on SQLite, runs the demo and compares it with `demo/output.txt`, and runs the suite again on a Postgres 16 service container.

## Demo

```bash
python demo/run_demo.py
```

The demo runs the real app in process against a throwaway SQLite file, fully offline. It shows readiness failing before the migration and passing after, auth rejecting bad keys, 24 events from four sources landing in one table, a replay being recognised, a bad payload being rejected with reasons, the worker draining the queue (including one job that goes to `dead`), and a sample of the JSON log lines. It talks to the app through FastAPI's test client and drives the worker by hand so the output is in a fixed order; the HTTP server and the worker thread are exercised by the steps under [Run it](#run-it). Captured output is in [`demo/output.txt`](demo/output.txt); `python demo/run_demo.py --save` regenerates it.

## Project layout

```text
app/
  config.py         settings from the environment, API key parsing
  db.py             engine factory and table definitions
  migrate.py        SQL migration runner
  normalizers.py    one payload model and mapper per source
  ingest.py         record + job in one transaction, idempotent
  enrich.py         rule based priority and tags (pure functions)
  jobs.py           queue, handlers, worker
  notify.py         mock and webhook notifiers
  auth.py           API key dependency
  logging_setup.py  JSON log formatter, request id
  api.py            routes
  main.py           application factory
migrations/         numbered SQL files
scripts/            migrate.py, seed.py
seeds/events.json   synthetic events
demo/               run_demo.py, output.txt
tests/
```

## Background

The shape (keyed ingestion from form, text, phone and inbox into one record table, with database backed jobs doing the follow up) is one the author uses in day to day systems for small service businesses. This repository is a clean rewrite of that pattern with synthetic data. Nothing from those systems is included, and this code itself has not served production traffic.

## Limitations

What is mocked or invented:

- **The four payload shapes are invented.** `webform`, `sms`, `call` and `email` are plausible JSON shapes, not any provider's real webhook format. No real form tool, SMS gateway, phone system or mail service has ever posted to this API. Real providers often send form encoded bodies and sign their requests; expect to write a normaliser per provider.
- **Alerts go to a mock by default.** The notifier keeps the message in memory and the job writes an `outbox` row. The webhook notifier is tested only against a fake HTTP transport; it has never posted to a real chat or paging service.
- **The triage is keyword rules.** `enrich.py` flags words like "leak" and "urgent". It will miss things and it will flag things it should not ("not urgent" is flagged). It is a placeholder for whatever classification you actually need.
- **The data is synthetic.** 24 seed events, written by hand for the demo.

What has not been proven:

- **No load or concurrency testing.** Nothing here has been run under production load and no throughput has been measured. The suite passes on Postgres 16 in CI, but always with a single worker. The claim step is a conditional UPDATE (not `SKIP LOCKED`) that is meant to be safe with several workers; no test runs two workers at once.
- **Jobs are at least once.** A handler can run twice after a crash, so handlers must be safe to repeat. The webhook notifier can deliver a duplicate if the process dies between the POST and the commit, and a job that outlives `JOB_VISIBILITY_TIMEOUT_SECONDS` is handed to a second worker.
- **The queue is a table, polled.** One job at a time per worker, no priorities, no scheduling beyond `run_after`. It is not a replacement for a broker at high volume.
- **SQLite is for the demo.** It has one writer at a time. Use Postgres for anything with more than one process.
- **`/readyz` only sees the in process worker.** With `WORKER_ENABLED=false` and a separate `python -m app worker`, readiness does not know whether that worker is alive.

What is missing:

- **Auth is static API keys.** No per key scopes, no rotation endpoint, no rate limiting, no request size limit beyond the 500 event batch cap. Put it behind a reverse proxy that handles TLS and limits.
- **Inbound webhooks are not signature checked.** Callers authenticate with an API key only.
- **Phone numbers assume North America.** A 10 digit number (or 11 digits starting with 1) without a `+` is stored as `+1...`; anything else needs the `+` and country code or the phone is dropped (the original stays in `raw`).
- **No contact matching.** The same person arriving by text and by email is two unrelated rows; nothing links or merges contacts.
- **Migrations are forward only**, and there is no data retention: `raw` payloads are kept forever unless you prune them.
- **Versions are pinned.** `requirements.txt` holds the versions the suite passes on. The test client prints an `httpx` deprecation warning on current Starlette, which is filtered in `pytest.ini`.

License: MIT, see the LICENSE file.
