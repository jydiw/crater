from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class TrackTags:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    release_year: int | None = None
    release_month: int | None = None
    release_day: int | None = None
    track_no: int | None = None
    genre: str | None = None
    label: str | None = None
    key: str | None = None
    bpm: float | None = None
    recording_mbid: str | None = None
    release_mbid: str | None = None
    earliest_mbid: str | None = None


# Fields written to/from file tags (ordered for CSV readability)
TAGGABLE_FIELDS: tuple[str, ...] = (
    "title",
    "artist",
    "album",
    "release_year",
    "release_month",
    "release_day",
    "track_no",
    "genre",
    "label",
    "key",
    "bpm",
    "recording_mbid",
    "release_mbid",
    "earliest_mbid",
)


Decision = Literal["accept", "reject", "review"]
Confidence = Literal["high", "medium", "low", "review"]


@dataclass
class TrackDiff:
    """One track's proposed tag changes. CSV rendering is two rows (before/after)."""
    track_id: int
    path: Path
    decision: Decision
    confidence: Confidence
    before: dict[str, str | None]
    after: dict[str, str | None]
    notes: str = ""

    def changed_fields(self) -> list[tuple[str, str | None, str | None]]:
        return [
            (f, self.before.get(f), self.after.get(f))
            for f in TAGGABLE_FIELDS
            if self.before.get(f) != self.after.get(f)
        ]


@dataclass
class ApplyResult:
    batch_id: int
    applied: int = 0
    skipped_review: int = 0
    skipped_drift: list[tuple[int, str]] = field(default_factory=list)
    skipped_missing: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RevertResult:
    batch_id: int
    reverted: int = 0
    skipped_drift: list[tuple[int, str]] = field(default_factory=list)
    skipped_missing: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    track_id: int
    field: str
    old_value: str | None
    new_value: str | None
    changed_at: int
    source: str
    batch_id: int
