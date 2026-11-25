CREATE TABLE source_cache (
    source     TEXT NOT NULL,
    cache_key  TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (source, cache_key)
) STRICT;

INSERT INTO source_cache (source, cache_key, payload, fetched_at)
    SELECT 'mb-recording', recording_mbid, payload, fetched_at FROM mb_cache;

DROP TABLE mb_cache;
