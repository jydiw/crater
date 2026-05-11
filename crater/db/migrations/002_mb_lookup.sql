ALTER TABLE track ADD COLUMN mb_confidence REAL;
ALTER TABLE track ADD COLUMN tags_reviewed_at INTEGER;

CREATE TABLE mb_cache (
    recording_mbid TEXT PRIMARY KEY,
    payload        TEXT NOT NULL,
    fetched_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
) STRICT;
