"""Multi-source field merge.

Each source proposes values for the fields it can speak to; `merge_proposals`
picks a winner per field from a priority list. A source is asked once per track
for every field it might answer, so an adapter can hide a single API call behind
`propose` without the merge loop fanning out one request per field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from crater.tags.models import TAGGABLE_FIELDS


@dataclass(frozen=True)
class TrackContext:
    """What a source gets to identify the track.

    `notes` is append-only: sources add human-readable lines (why they matched,
    what they had to guess) that end up in the diff CSV.
    """

    path: Path
    before: dict[str, str | None]
    duration_s: float | None
    recording_mbid: str | None
    release_mbid: str | None
    notes: list[str] = field(default_factory=list)


class MetadataSource(Protocol):
    name: str

    def propose(
        self, ctx: TrackContext, fields: frozenset[str]
    ) -> dict[str, str | None]: ...


@dataclass
class StaticSource:
    """A source over an already-fetched payload's tags. No network."""

    name: str
    values: dict[str, str | None]

    def propose(
        self, ctx: TrackContext, fields: frozenset[str]
    ) -> dict[str, str | None]:
        return {f: self.values[f] for f in fields if f in self.values}


# Per-field source priority, highest first. Empty tuple = never proposed, so the
# file's existing value always survives.
DEFAULT_FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    "title": ("musicbrainz",),
    "artist": ("musicbrainz",),
    "album": ("musicbrainz",),
    "release_year": ("musicbrainz",),
    "release_month": ("musicbrainz",),
    "release_day": ("musicbrainz",),
    "track_no": (),
    "genre": ("deezer", "itunes", "musicbrainz"),
    "label": ("musicbrainz",),
    "key": (),
    "bpm": ("deezer",),
    "recording_mbid": ("musicbrainz",),
    "release_mbid": ("musicbrainz",),
    "earliest_mbid": ("musicbrainz",),
}


@dataclass
class MergeResult:
    after: dict[str, str | None]
    winners: dict[str, str]


def merge_proposals(
    ctx: TrackContext,
    field_order: dict[str, tuple[str, ...]],
    sources: dict[str, MetadataSource],
    *,
    preserve_if_set: tuple[str, ...],
) -> MergeResult:
    """Resolve each taggable field to its highest-priority available proposal.

    Sources named in `field_order` but absent from `sources` are skipped: the
    priority table lists every source crater knows about, not just the ones
    configured for this run.
    """
    after = dict(ctx.before)
    winners: dict[str, str] = {}
    resolved: set[str] = set()
    proposals: dict[str, dict[str, str | None]] = {}

    def preserved(name: str) -> bool:
        return name in preserve_if_set and bool(ctx.before.get(name))

    def wanted_from(source_name: str) -> frozenset[str]:
        return frozenset(
            f
            for f in TAGGABLE_FIELDS
            if source_name in field_order.get(f, ())
            and f not in resolved
            and not preserved(f)
        )

    for field_name in TAGGABLE_FIELDS:
        if preserved(field_name):
            continue
        for source_name in field_order.get(field_name, ()):
            source = sources.get(source_name)
            if source is None:
                continue
            if source_name not in proposals:
                proposals[source_name] = source.propose(ctx, wanted_from(source_name))
            value = proposals[source_name].get(field_name)
            if value is None:
                continue
            resolved.add(field_name)
            after[field_name] = value
            if value != ctx.before.get(field_name):
                winners[field_name] = source.name
            break

    return MergeResult(after=after, winners=winners)
