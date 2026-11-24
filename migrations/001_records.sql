-- One row per inbound event, whatever system it came from.
-- (source, external_id) is the idempotency key: replays do not create rows.
CREATE TABLE records (
    id TEXT NOT NULL PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at {{TIMESTAMP}} NOT NULL,
    received_at {{TIMESTAMP}} NOT NULL,
    contact_name TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    summary TEXT NOT NULL DEFAULT '',
    raw {{JSON}} NOT NULL,
    CONSTRAINT uq_records_source_external UNIQUE (source, external_id)
);

CREATE INDEX ix_records_occurred_at ON records (occurred_at);

CREATE INDEX ix_records_contact_phone ON records (contact_phone);
