-- Reference DDL. Authoritative migration is in migrations/001_initial.sql.
-- All timestamps are INTEGER unix epoch seconds.
-- mtime_ns is nanoseconds from os.stat().st_mtime_ns (drift detection only).

CREATE TABLE track (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL UNIQUE,
    content_hash    TEXT UNIQUE,
    size_bytes      INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    format          TEXT NOT NULL,

    title           TEXT,
    artist          TEXT,
    album           TEXT,
    track_no        INTEGER,
    genre           TEXT,
    label           TEXT,
    key             TEXT,
    bpm             REAL,

    release_year    INTEGER,
    release_month   INTEGER,
    release_day     INTEGER,

    recording_mbid  TEXT,
    release_mbid    TEXT,
    earliest_mbid   TEXT,

    acquired_at     INTEGER NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
) STRICT;
CREATE INDEX idx_track_content_hash   ON track(content_hash);
CREATE INDEX idx_track_recording_mbid ON track(recording_mbid);
CREATE INDEX idx_track_acquired_at    ON track(acquired_at);
CREATE INDEX idx_track_release_date   ON track(release_year, release_month, release_day);

CREATE TABLE batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    source      TEXT NOT NULL,
    csv_path    TEXT,
    reverts     INTEGER REFERENCES batch(id)
) STRICT;

CREATE TABLE change (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    source      TEXT NOT NULL,
    batch_id    INTEGER NOT NULL REFERENCES batch(id)
) STRICT;
CREATE INDEX idx_change_track ON change(track_id, changed_at);
CREATE INDEX idx_change_batch ON change(batch_id);

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
) STRICT;
