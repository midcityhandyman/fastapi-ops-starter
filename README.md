# fastapi-ops-starter

A small service business gets events from a web form, a text line, a phone system and an inbox, each in its own shape, and nobody has one place to look.
This is the slim API that sits in front of all of them: one keyed ingestion endpoint, one record table, and background jobs that act on what arrives.

## What it does

- **API key auth.** Every `/v1` route needs a key (`X-API-Key` or `Authorization: Bearer`). Keys are stored and compared as SHA-256 digests in constant time, and the log shows the key's name, never the key.
- **Ingestion that normalises.** `POST /v1/ingest/{source}` accepts the source's own payload (`webform`, `sms`, `call`, `email`) and writes one row to `records` with the same fields every time: who, when (UTC), what kind of event, a summary, and the untouched original in `raw`.
- **Idempotent by design.** `(source, external_id)` is unique. A replayed webhook returns `200 duplicate` and creates nothing.
- **Background jobs without a broker.** Jobs are rows in the same database, written in the same transaction as the record. A worker thread claims them, retries with exponential backoff, and parks hopeless ones as `dead` where you can see and retry them.
- **Health and readiness.** `/healthz` says the process is up. `/readyz` checks the database, pending migrations and the worker heartbeat, and returns 503 with the reason when any of them fail.
- **Structured logging.** One JSON object per line, with a request id that is accepted from `X-Request-ID` or generated, and echoed back on the response.
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

Python 3.12, nothing else. No `.env` is needed for a local run.

```bash
pip install -r requirements.txt
python scripts/migrate.py --seed
python -m app serve
```

The server listens on `http://127.0.0.1:8000`. With no `OPS_API_KEYS` set and `APP_ENV` not `production`, it accepts the development key `dev-local-key` and says so in the log. Then:

```bash
curl http://127.0.0.1:8000/readyz
curl -H "X-API-Key: dev-local-key" "http://127.0.0.1:8000/v1/records?priority=high"
curl -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{"message_sid":"SMfake9001","from":"+12025550199","body":"urgent, the tap is leaking","timestamp":1793628000}' \
  http://127.0.0.1:8000/v1/ingest/sms
```

Interactive API docs are at `/docs`.

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

76 tests, about three seconds, no network and no keys. They cover auth on every `/v1` route, each normaliser, idempotent ingest, batch ingest, record filters, job retry and backoff with a fake clock, rollback of a failed handler's writes, stale job recovery, the live worker thread, the migration runner (ordering, checksums, schema matching the table definitions), readiness, config parsing, log format and the seed data.

To run the same suite against Postgres, point `TEST_DATABASE_URL` at an empty database (the tests drop and recreate their tables):

```bash
pip install -r requirements-postgres.txt
TEST_DATABASE_URL=postgresql://ops:password@localhost:5432/ops_test python -m pytest
```

CI (`.github/workflows/ci.yml`) runs the suite on SQLite, runs the demo, and runs the suite again on a Postgres 16 service container.

## Demo

```bash
python demo/run_demo.py
```

The demo runs the real app in process against a throwaway SQLite file, fully offline. It shows readiness failing before the migration and passing after, auth rejecting bad keys, 24 events from four sources landing in one table, a replay being recognised, a bad payload being rejected with reasons, the worker draining the queue (including one job that goes to `dead`), and a sample of the JSON log lines. Captured output is in [`demo/output.txt`](demo/output.txt); `python demo/run_demo.py --save` regenerates it.

A slice of that output:

```text
== 4. Replays and bad payloads ==
same webform event again -> 200 duplicate (no new row)
sms without required fields -> 422
  problem: message_sid: Field required

== 5. Background jobs ==
queue before: {'queued': 25}
worker ran 33 jobs
queue after:  {'dead': 1, 'done': 32}
by kind:      {'enrich_record': 24, 'notify': 8, 'send_fax': 1}
```

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

## Where this pattern runs

This starter is drawn from systems that run two small service companies day to day. There, the same shape (keyed ingestion from the phone, text, form and inbox channels into one record table, with database backed jobs doing the follow up work) is what lets a small team see every inbound contact in one place and trust that nothing was dropped or counted twice. The code here is a clean rewrite of the pattern with synthetic data; nothing from those systems is included.

## Limitations

- **The triage is keyword rules.** `enrich.py` flags "leak" and "urgent"; it will miss things and it will flag things it should not. It is a placeholder for whatever classification you actually need.
- **Jobs are at least once.** A handler can run twice after a crash, so handlers must be safe to repeat. The webhook notifier in particular can deliver a duplicate if the process dies between the POST and the commit.
- **The queue is a table, polled.** Fine for tens of jobs a second with a one second poll. It is not a replacement for a real broker at high volume, and there is no priority or scheduling beyond `run_after`.
- **SQLite is for the demo.** It works with the in process worker, but it has one writer at a time. Use Postgres for anything with more than one process.
- **Postgres was tested, not load tested.** The full suite passed against Postgres 16 in a local container, and the CI workflow has a job that repeats that on every push. Nothing here has been run under concurrent production load, and the claim step uses a conditional UPDATE rather than `SKIP LOCKED`.
- **Auth is static API keys.** No per key scopes, no rotation endpoint, no rate limiting, no request size limit beyond the 500 event batch cap. Put it behind a reverse proxy that handles TLS and limits.
- **Inbound webhooks are not signature checked.** Callers authenticate with an API key. If a provider signs its webhooks, verify that in the normaliser's route before trusting the payload.
- **Migrations are forward only**, and there is no data retention: `raw` payloads are kept forever unless you prune them.
- **The test client prints a deprecation warning** about `httpx` on current Starlette. It is filtered in `pytest.ini`; the pinned versions in `requirements.txt` are the ones the suite passes on.

License: MIT, see the LICENSE file.
