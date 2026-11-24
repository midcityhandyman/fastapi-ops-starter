-- Database backed job queue. Status moves queued -> running -> done,
-- back to queued on a retryable failure, and to dead when attempts run out.
CREATE TABLE jobs (
    id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL,
    payload {{JSON}} NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 4,
    run_after {{TIMESTAMP}} NOT NULL,
    locked_at {{TIMESTAMP}},
    last_error TEXT,
    record_id TEXT REFERENCES records (id),
    created_at {{TIMESTAMP}} NOT NULL,
    finished_at {{TIMESTAMP}}
);

CREATE INDEX ix_jobs_status_run_after ON jobs (status, run_after);

-- Every notification the system sent (or pretended to send, in mock mode).
CREATE TABLE outbox (
    id TEXT NOT NULL PRIMARY KEY,
    channel TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    delivered_via TEXT NOT NULL,
    record_id TEXT REFERENCES records (id),
    created_at {{TIMESTAMP}} NOT NULL
);
