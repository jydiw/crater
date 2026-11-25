"""Shared fixtures: in-memory audio file generation for tests.

Real files, no copyrighted music. Uses soundfile + numpy to write
short silent FLAC files with known tag values via mutagen.
"""

import sqlite3
from pathlib import Path

import mutagen.flac
import numpy as np
import pytest
import soundfile as sf

from crater.db.connection import open_db


# ---------------------------------------------------------------------------
# Audio generation helpers
# ---------------------------------------------------------------------------

def _write_silent_flac(path: Path, tags: dict[str, str] | None = None) -> Path:
    """Write a 1-second silent FLAC file with optional Vorbis tags."""
    samples = np.zeros((44100,), dtype=np.float32)
    sf.write(str(path), samples, 44100, format="FLAC")
    if tags:
        audio = mutagen.flac.FLAC(path)
        if audio.tags is None:
            audio.add_tags()
        for key, val in tags.items():
            audio.tags[key] = [val]  # type: ignore[index]
        audio.save()
    return path


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_library(tmp_path: Path) -> Path:
    """A temporary library root with crater.db initialized."""
    (tmp_path / "store").mkdir()
    (tmp_path / "staging").mkdir()
    return tmp_path


@pytest.fixture()
def db(tmp_library: Path) -> sqlite3.Connection:
    conn = open_db(tmp_library / "crater.db")
    yield conn
    conn.close()


@pytest.fixture()
def flac_with_tags(tmp_path: Path) -> Path:
    """A FLAC file with a known set of tags."""
    return _write_silent_flac(
        tmp_path / "test_track.flac",
        tags={
            "title": "Test Track",
            "artist": "Test Artist",
            "album": "Test Album",
            "date": "1996-09-23",
            "tracknumber": "3",
            "genre": "Electronic",
            "organization": "Test Label",
            "bpm": "120",
        },
    )


@pytest.fixture()
def flac_no_tags(tmp_path: Path) -> Path:
    """A FLAC file with no tags."""
    return _write_silent_flac(tmp_path / "notags.flac")
