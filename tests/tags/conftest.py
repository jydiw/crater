"""Shared setup for the revert/history/delete tests: a registered, tagged track.

`apply_change` runs a real CSV apply over the fixture file, so the tests that
follow start from a genuine batch + change history.
"""

import itertools
import sqlite3
from collections.abc import Callable
from pathlib import Path

import mutagen.id3
import mutagen.mp3
import pytest

from crater.db.repo import TrackRepo
from crater.tags.api import apply_diff, generate_diff, write_diff_csv
from crater.tags.io import read_tags
from crater.tags.models import TAGGABLE_FIELDS, ApplyResult, ArtChange, TrackDiff
from crater.tags.values import current_str

# Never decoded — embedding and sniffing only ever look at the magic bytes.
_STAGED_PNG = b"\x89PNG\r\n\x1a\n" + b"staged image" * 4


def _write_silent_mp3(path: Path, tags: dict[str, str] | None = None) -> Path:
    """Write a minimal MPEG1 L3 file with optional ID3 tags."""
    # Two 417-byte frames (mutagen needs min 2 consecutive frames to parse the stream)
    # MPEG1 L3 128kbps 44100Hz Stereo header + null audio data
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame + frame)
    if tags:
        audio = mutagen.mp3.MP3(path)
        if audio.tags is None:
            audio.add_tags()
        id3 = audio.tags
        assert id3 is not None
        _frames: dict[str, type] = {
            "title": mutagen.id3.TIT2,
            "artist": mutagen.id3.TPE1,
            "album": mutagen.id3.TALB,
            "tracknumber": mutagen.id3.TRCK,
        }
        for key, val in tags.items():
            frame_cls = _frames[key]
            id3[frame_cls.__name__] = frame_cls(encoding=3, text=[val])
        audio.save()
    return path


@pytest.fixture()
def mp3_with_tags(tmp_path: Path) -> Path:
    """A minimal MP3 file with a known set of tags."""
    return _write_silent_mp3(
        tmp_path / "test_track.mp3",
        tags={
            "title": "MP3 Track",
            "artist": "MP3 Artist",
            "album": "MP3 Album",
            "tracknumber": "5",
        },
    )


@pytest.fixture()
def track_id(db: sqlite3.Connection, flac_with_tags: Path) -> int:
    """Register flac_with_tags in the DB and return its track id."""
    generate_diff(db, [flac_with_tags])
    row = TrackRepo(db).get_by_path(flac_with_tags.resolve())
    assert row is not None
    return int(row["id"])


@pytest.fixture()
def staged_art(tmp_path: Path) -> Path:
    """A cover image fetched at diff time, sitting in the review art dir."""
    path = tmp_path / "review" / "art" / "rel-abc.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(_STAGED_PNG)
    return path


@pytest.fixture()
def apply_change(
    db: sqlite3.Connection, flac_with_tags: Path, track_id: int, tmp_path: Path
) -> Callable[..., ApplyResult]:
    """Apply a CSV diff over the fixture track's current file state."""
    counter = itertools.count()

    def _apply(
        art: ArtChange | None = None, **after_overrides: str | None
    ) -> ApplyResult:
        tags = read_tags(flac_with_tags)
        before = {f: current_str(tags, f) for f in TAGGABLE_FIELDS}
        diff = TrackDiff(
            track_id=track_id,
            path=flac_with_tags.resolve(),
            decision="accept",
            confidence="high",
            before=before,
            after=before | after_overrides,
            art=art,
        )
        csv = tmp_path / f"diff-{next(counter)}.csv"
        write_diff_csv([diff], csv)
        return apply_diff(db, csv)

    return _apply
