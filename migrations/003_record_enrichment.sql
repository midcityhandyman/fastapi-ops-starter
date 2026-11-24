-- Fields filled in by the enrich_record background job.
ALTER TABLE records ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal';

ALTER TABLE records ADD COLUMN tags {{JSON}};

ALTER TABLE records ADD COLUMN enriched_at {{TIMESTAMP}};

CREATE INDEX ix_records_priority ON records (priority);
