"""Round-trip tests for read_tags / write_tags on real MP3 and FLAC files.

The tag library is the independent source of truth: crater writes, mutagen reads
the raw tag under the name in the CLAUDE.md data model table, and the reverse.
"""

from __future__ import annotations

from pathlib import Path

import mutagen.flac
import mutagen.id3
import mutagen.mp3
import pytest

from crater.tags.io import read_tags, write_tags
from crater.tags.models import TrackTags
from tests.conftest import _write_silent_flac
from tests.tags.conftest import _write_silent_mp3

_REC = "11111111-1111-1111-1111-111111111111"
_REL = "22222222-2222-2222-2222-222222222222"
_EARLIEST = "33333333-3333-3333-3333-333333333333"

_FULL = TrackTags(
    title="Windowlicker",
    artist="Aphex Twin",
    album="Windowlicker EP",
    release_year=1999,
    release_month=3,
    release_day=22,
    track_no=1,
    genre="IDM",
    label="Warp",
    key="Fm - 4A",
    bpm=127.0,
    recording_mbid=_REC,
    release_mbid=_REL,
    earliest_mbid=_EARLIEST,
)


def _mp3(tmp_path: Path) -> Path:
    return _write_silent_mp3(tmp_path / "track.mp3")


def _flac(tmp_path: Path) -> Path:
    return _write_silent_flac(tmp_path / "track.flac")


def _id3(path: Path) -> mutagen.id3.ID3:
    tags = mutagen.mp3.MP3(path).tags
    assert tags is not None
    return tags


def _vorbis(path: Path) -> mutagen.flac.VCFLACDict:
    tags = mutagen.flac.FLAC(path).tags
    assert tags is not None
    return tags


def _add_id3(path: Path, *frames: mutagen.id3.Frame) -> None:
    audio = mutagen.mp3.MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    for frame in frames:
        audio.tags.add(frame)
    audio.save()


def _add_vorbis(path: Path, tags: dict[str, str]) -> None:
    audio = mutagen.flac.FLAC(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    for key, val in tags.items():
        audio.tags[key] = [val]
    audio.save()


# ---------------------------------------------------------------------------
# MP3: crater writes, mutagen reads
# ---------------------------------------------------------------------------


def test_mp3_write_puts_every_field_under_its_id3_frame(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    write_tags(path, _FULL)
    id3 = _id3(path)
    assert id3["TIT2"].text == ["Windowlicker"]
    assert id3["TPE1"].text == ["Aphex Twin"]
    assert id3["TALB"].text == ["Windowlicker EP"]
    assert str(id3["TDRC"]) == "1999-03-22"
    assert id3["TRCK"].text == ["1"]
    assert id3["TCON"].text == ["IDM"]
    assert id3["TPUB"].text == ["Warp"]
    assert id3["TKEY"].text == ["Fm - 4A"]
    assert id3["TBPM"].text == ["127"]
    assert id3["UFID:http://musicbrainz.org"].data == _REC.encode()
    assert id3["TXXX:MusicBrainz Album Id"].text == [_REL]
    assert id3["TXXX:MusicBrainz Earliest Id"].text == [_EARLIEST]


def test_mp3_write_then_read_returns_every_field(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    write_tags(path, _FULL)
    assert read_tags(path) == _FULL


@pytest.mark.parametrize(
    ("year", "month", "day", "raw"),
    [
        (1999, None, None, "1999"),
        (1999, 3, None, "1999-03"),
        (1999, 3, 22, "1999-03-22"),
    ],
)
def test_mp3_partial_release_date_written_at_its_precision(
    tmp_path: Path, year: int, month: int | None, day: int | None, raw: str
) -> None:
    path = _mp3(tmp_path)
    write_tags(path, TrackTags(release_year=year, release_month=month, release_day=day))
    assert str(_id3(path)["TDRC"]) == raw


def test_mp3_cleared_field_removes_its_frame(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    write_tags(path, _FULL)
    write_tags(path, TrackTags(title="Windowlicker"))
    id3 = _id3(path)
    assert id3["TIT2"].text == ["Windowlicker"]
    for frame_id in (
        "TPE1",
        "TALB",
        "TDRC",
        "TRCK",
        "TCON",
        "TPUB",
        "TKEY",
        "TBPM",
        "UFID:http://musicbrainz.org",
        "TXXX:MusicBrainz Album Id",
        "TXXX:MusicBrainz Earliest Id",
    ):
        assert frame_id not in id3, frame_id


def test_mp3_write_leaves_unmanaged_frames_untouched(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    _add_id3(
        path,
        mutagen.id3.COMM(encoding=3, lang="eng", desc="", text=["bought at shop"]),
        mutagen.id3.TXXX(encoding=3, desc="ENERGY", text=["7"]),
        mutagen.id3.TCOM(encoding=3, text=["Richard D. James"]),
    )
    unmanaged = ("COMM::eng", "TXXX:ENERGY", "TCOM")
    before = {k: _id3(path)[k]._writeData() for k in unmanaged}

    write_tags(path, _FULL)
    write_tags(path, TrackTags())

    after = {k: _id3(path)[k]._writeData() for k in unmanaged}
    assert after == before


# ---------------------------------------------------------------------------
# MP3: mutagen writes, crater reads
# ---------------------------------------------------------------------------


def test_mp3_read_returns_every_field_from_raw_frames(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    _add_id3(
        path,
        mutagen.id3.TIT2(encoding=3, text=["Windowlicker"]),
        mutagen.id3.TPE1(encoding=3, text=["Aphex Twin"]),
        mutagen.id3.TALB(encoding=3, text=["Windowlicker EP"]),
        mutagen.id3.TDRC(encoding=3, text=["1999-03-22"]),
        mutagen.id3.TRCK(encoding=3, text=["1"]),
        mutagen.id3.TCON(encoding=3, text=["IDM"]),
        mutagen.id3.TPUB(encoding=3, text=["Warp"]),
        mutagen.id3.TKEY(encoding=3, text=["Fm - 4A"]),
        mutagen.id3.TBPM(encoding=3, text=["127"]),
        mutagen.id3.UFID(owner="http://musicbrainz.org", data=_REC.encode()),
        mutagen.id3.TXXX(encoding=3, desc="MusicBrainz Album Id", text=[_REL]),
        mutagen.id3.TXXX(encoding=3, desc="MusicBrainz Earliest Id", text=[_EARLIEST]),
    )
    assert read_tags(path) == _FULL


@pytest.mark.parametrize(
    ("raw", "year", "month", "day"),
    [
        ("1999", 1999, None, None),
        ("1999-03", 1999, 3, None),
        ("1999-03-22", 1999, 3, 22),
    ],
)
def test_mp3_read_partial_release_date(
    tmp_path: Path, raw: str, year: int, month: int | None, day: int | None
) -> None:
    path = _mp3(tmp_path)
    _add_id3(path, mutagen.id3.TDRC(encoding=3, text=[raw]))
    tags = read_tags(path)
    assert (tags.release_year, tags.release_month, tags.release_day) == (
        year,
        month,
        day,
    )


def test_mp3_read_drops_track_total(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    _add_id3(path, mutagen.id3.TRCK(encoding=3, text=["4/12"]))
    assert read_tags(path).track_no == 4


def test_mp3_read_with_no_tags_is_empty(tmp_path: Path) -> None:
    assert read_tags(_mp3(tmp_path)) == TrackTags()


# ---------------------------------------------------------------------------
# FLAC: crater writes, mutagen reads
# ---------------------------------------------------------------------------


def test_flac_write_puts_every_field_under_its_vorbis_name(tmp_path: Path) -> None:
    path = _flac(tmp_path)
    write_tags(path, _FULL)
    vc = _vorbis(path)
    assert vc["title"] == ["Windowlicker"]
    assert vc["artist"] == ["Aphex Twin"]
    assert vc["album"] == ["Windowlicker EP"]
    assert vc["date"] == ["1999-03-22"]
    assert vc["tracknumber"] == ["1"]
    assert vc["genre"] == ["IDM"]
    assert vc["organization"] == ["Warp"]
    assert vc["initialkey"] == ["Fm - 4A"]
    assert vc["bpm"] == ["127"]
    assert vc["musicbrainz_trackid"] == [_REC]
    assert vc["musicbrainz_albumid"] == [_REL]
    assert vc["musicbrainz_earliestid"] == [_EARLIEST]


def test_flac_write_then_read_returns_every_field(tmp_path: Path) -> None:
    path = _flac(tmp_path)
    write_tags(path, _FULL)
    assert read_tags(path) == _FULL


@pytest.mark.parametrize(
    ("year", "month", "day", "raw"),
    [
        (1999, None, None, "1999"),
        (1999, 3, None, "1999-03"),
        (1999, 3, 22, "1999-03-22"),
    ],
)
def test_flac_partial_release_date_written_at_its_precision(
    tmp_path: Path, year: int, month: int | None, day: int | None, raw: str
) -> None:
    path = _flac(tmp_path)
    write_tags(path, TrackTags(release_year=year, release_month=month, release_day=day))
    assert _vorbis(path)["date"] == [raw]


def test_flac_cleared_field_removes_its_comment(tmp_path: Path) -> None:
    path = _flac(tmp_path)
    write_tags(path, _FULL)
    write_tags(path, TrackTags(title="Windowlicker"))
    vc = _vorbis(path)
    assert vc["title"] == ["Windowlicker"]
    for key in (
        "artist",
        "album",
        "date",
        "tracknumber",
        "genre",
        "organization",
        "initialkey",
        "bpm",
        "musicbrainz_trackid",
        "musicbrainz_albumid",
        "musicbrainz_earliestid",
    ):
        assert key not in vc, key


def test_flac_write_leaves_unmanaged_comments_untouched(tmp_path: Path) -> None:
    path = _flac(tmp_path)
    _add_vorbis(
        path,
        {"comment": "bought at shop", "energy": "7", "composer": "Richard D. James"},
    )
    unmanaged = ("comment", "energy", "composer")
    before = {k: _vorbis(path)[k] for k in unmanaged}

    write_tags(path, _FULL)
    write_tags(path, TrackTags())

    after = {k: _vorbis(path)[k] for k in unmanaged}
    assert (
        after
        == before
        == {
            "comment": ["bought at shop"],
            "energy": ["7"],
            "composer": ["Richard D. James"],
        }
    )


# ---------------------------------------------------------------------------
# FLAC: mutagen writes, crater reads
# ---------------------------------------------------------------------------


def test_flac_read_returns_every_field_from_raw_comments(tmp_path: Path) -> None:
    path = _flac(tmp_path)
    _add_vorbis(
        path,
        {
            "title": "Windowlicker",
            "artist": "Aphex Twin",
            "album": "Windowlicker EP",
            "date": "1999-03-22",
            "tracknumber": "1",
            "genre": "IDM",
            "organization": "Warp",
            "initialkey": "Fm - 4A",
            "bpm": "127",
            "musicbrainz_trackid": _REC,
            "musicbrainz_albumid": _REL,
            "musicbrainz_earliestid": _EARLIEST,
        },
    )
    assert read_tags(path) == _FULL


@pytest.mark.parametrize(
    ("raw", "year", "month", "day"),
    [
        ("1999", 1999, None, None),
        ("1999-03", 1999, 3, None),
        ("1999-03-22", 1999, 3, 22),
    ],
)
def test_flac_read_partial_release_date(
    tmp_path: Path, raw: str, year: int, month: int | None, day: int | None
) -> None:
    path = _flac(tmp_path)
    _add_vorbis(path, {"date": raw})
    tags = read_tags(path)
    assert (tags.release_year, tags.release_month, tags.release_day) == (
        year,
        month,
        day,
    )


def test_flac_read_drops_track_total(tmp_path: Path) -> None:
    path = _flac(tmp_path)
    _add_vorbis(path, {"tracknumber": "4/12"})
    assert read_tags(path).track_no == 4


def test_flac_read_with_no_tags_is_empty(tmp_path: Path) -> None:
    assert read_tags(_flac(tmp_path)) == TrackTags()
