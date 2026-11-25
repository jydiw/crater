"""Read and write audio file tags via mutagen."""

from pathlib import Path

import mutagen
import mutagen.flac
import mutagen.id3
import mutagen.mp3

from crater.tags.errors import TagIOError
from crater.tags.models import TrackTags

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(s: str | None) -> tuple[int | None, int | None, int | None]:
    if not s:
        return None, None, None
    parts = s.strip().split("-")
    try:
        year = int(parts[0]) if parts[0].isdigit() else None
        month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    except (ValueError, IndexError):
        return None, None, None
    return year, month, day


def _format_date(year: int | None, month: int | None, day: int | None) -> str | None:
    if year is None:
        return None
    if month is None:
        return str(year)
    if day is None:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_track_no(s: str | None) -> int | None:
    """Parse TRCK/tracknumber, discarding any "/total" suffix."""
    if not s:
        return None
    try:
        return int(s.split("/")[0].strip())
    except ValueError:
        return None


# Field -> (tag name crater writes, other names for the same field).
# "date" stands for release_year/month/day together. Row order is write order.
_TagTable = dict[str, tuple[str, tuple[str, ...]]]

_ID3_NAMES: _TagTable = {
    "title": ("TIT2", ()),
    "artist": ("TPE1", ()),
    "album": ("TALB", ()),
    "track_no": ("TRCK", ()),
    "genre": ("TCON", ()),
    "label": ("TPUB", ()),
    "key": ("TKEY", ()),
    "date": ("TDRC", ()),
    "bpm": ("TBPM", ()),
    "recording_mbid": ("UFID:http://musicbrainz.org", ()),
    "release_mbid": ("TXXX:MusicBrainz Album Id", ()),
    "earliest_mbid": ("TXXX:MusicBrainz Earliest Id", ()),
}

_VORBIS_NAMES: _TagTable = {
    "title": ("title", ()),
    "artist": ("artist", ()),
    "album": ("album", ()),
    "track_no": ("tracknumber", ()),
    "genre": ("genre", ()),
    "label": ("organization", ()),
    "key": ("initialkey", ()),
    "date": ("date", ()),
    "bpm": ("bpm", ()),
    "recording_mbid": ("musicbrainz_trackid", ()),
    "release_mbid": ("musicbrainz_albumid", ()),
    "earliest_mbid": ("musicbrainz_earliestid", ()),
}


def _from_tag_values(values: dict[str, str | None]) -> TrackTags:
    bpm_str = values["bpm"]
    bpm: float | None = None
    if bpm_str:
        try:
            bpm = float(bpm_str)
        except ValueError:
            pass

    year, month, day = _parse_date(values["date"])

    return TrackTags(
        title=values["title"],
        artist=values["artist"],
        album=values["album"],
        release_year=year,
        release_month=month,
        release_day=day,
        track_no=_parse_track_no(values["track_no"]),
        genre=values["genre"],
        label=values["label"],
        key=values["key"],
        bpm=bpm,
        recording_mbid=values["recording_mbid"],
        release_mbid=values["release_mbid"],
        earliest_mbid=values["earliest_mbid"],
    )


def _to_tag_values(tags: TrackTags) -> dict[str, str | None]:
    return {
        "title": tags.title,
        "artist": tags.artist,
        "album": tags.album,
        "track_no": str(tags.track_no) if tags.track_no is not None else None,
        "genre": tags.genre,
        "label": tags.label,
        "key": tags.key,
        "date": _format_date(tags.release_year, tags.release_month, tags.release_day),
        "bpm": str(int(tags.bpm)) if tags.bpm is not None else None,
        "recording_mbid": tags.recording_mbid,
        "release_mbid": tags.release_mbid,
        "earliest_mbid": tags.earliest_mbid,
    }


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "mp3"
    if suffix == ".flac":
        return "flac"
    raise TagIOError(f"unsupported format: {path.suffix!r}")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_tags(path: Path) -> TrackTags:
    fmt = detect_format(path)
    if fmt == "mp3":
        return _read_mp3(path)
    return _read_flac(path)


def read_duration(path: Path) -> float | None:
    """Stream length in seconds, or None when the file won't parse.

    Unlike tag reads this never raises: callers use duration as corroboration,
    and a file we can't measure just means "unknown", not "broken".
    """
    try:
        audio = mutagen.File(path)
        return float(audio.info.length) if audio is not None else None
    except Exception:
        return None


def _read_mp3(path: Path) -> TrackTags:
    try:
        audio = mutagen.mp3.MP3(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc

    tags = audio.tags
    if tags is None:
        return TrackTags()

    def read(name: str) -> str | None:
        frame = tags.get(name)
        if frame is None:
            return None
        kind = name.partition(":")[0]
        if kind == "UFID":
            try:
                val = frame.data.decode("utf-8")
            except UnicodeDecodeError:
                return None
        elif kind == "TXXX":
            val = frame.text[0] if frame.text else ""
        else:
            val = str(frame)
        return val.strip() or None

    return _from_tag_values({f: read(name) for f, (name, _) in _ID3_NAMES.items()})


def _read_flac(path: Path) -> TrackTags:
    try:
        audio = mutagen.flac.FLAC(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc

    tags = audio.tags

    def read(name: str) -> str | None:
        if tags is None:
            return None
        vals = tags.get(name)
        if not vals:
            return None
        return vals[0].strip() or None

    return _from_tag_values({f: read(name) for f, (name, _) in _VORBIS_NAMES.items()})


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_tags(path: Path, tags: TrackTags) -> None:
    fmt = detect_format(path)
    if fmt == "mp3":
        _write_mp3(path, tags)
    else:
        _write_flac(path, tags)


def _write_mp3(path: Path, tags: TrackTags) -> None:
    try:
        audio = mutagen.mp3.MP3(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc

    if audio.tags is None:
        audio.add_tags()
    id3 = audio.tags
    assert id3 is not None

    values = _to_tag_values(tags)
    for f, (name, _) in _ID3_NAMES.items():
        val = values[f]
        if val is not None:
            id3[name] = _id3_frame(name, val)
        elif name in id3:
            del id3[name]

    audio.save()


def _id3_frame(name: str, val: str) -> mutagen.id3.Frame:
    kind, _, desc = name.partition(":")
    if kind == "UFID":
        return mutagen.id3.UFID(owner=desc, data=val.encode("utf-8"))
    if kind == "TXXX":
        return mutagen.id3.TXXX(encoding=3, desc=desc, text=[val])
    return mutagen.id3.Frames[kind](encoding=3, text=[val])


def _write_flac(path: Path, tags: TrackTags) -> None:
    try:
        audio = mutagen.flac.FLAC(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc

    if audio.tags is None:
        audio.add_tags()
    vc = audio.tags
    assert vc is not None

    values = _to_tag_values(tags)
    for f, (name, _) in _VORBIS_NAMES.items():
        val = values[f]
        if val is not None:
            vc[name] = [val]
        elif name in vc:
            del vc[name]

    audio.save()
