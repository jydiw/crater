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

    def text(frame_id: str) -> str | None:
        frame = tags.get(frame_id)
        if frame is None:
            return None
        val = str(frame)
        return val.strip() or None

    def txxx(desc: str) -> str | None:
        frame = tags.get(f"TXXX:{desc}")
        if frame is None:
            return None
        val = frame.text[0] if frame.text else ""
        return val.strip() or None

    def ufid(owner: str) -> str | None:
        frame = tags.get(f"UFID:{owner}")
        if frame is None:
            return None
        try:
            return frame.data.decode("utf-8").strip() or None
        except UnicodeDecodeError:
            return None

    bpm_str = text("TBPM")
    bpm: float | None = None
    if bpm_str:
        try:
            bpm = float(bpm_str)
        except ValueError:
            pass

    year, month, day = _parse_date(text("TDRC"))
    track_no = _parse_track_no(text("TRCK"))

    return TrackTags(
        title=text("TIT2"),
        artist=text("TPE1"),
        album=text("TALB"),
        release_year=year,
        release_month=month,
        release_day=day,
        track_no=track_no,
        genre=text("TCON"),
        label=text("TPUB"),
        key=text("TKEY"),
        bpm=bpm,
        recording_mbid=ufid("http://musicbrainz.org"),
        release_mbid=txxx("MusicBrainz Album Id"),
        earliest_mbid=txxx("MusicBrainz Earliest Id"),
    )


def _read_flac(path: Path) -> TrackTags:
    try:
        audio = mutagen.flac.FLAC(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc

    tags = audio.tags

    def vorbis(key: str) -> str | None:
        if tags is None:
            return None
        vals = tags.get(key.lower())
        if not vals:
            return None
        return vals[0].strip() or None

    bpm_str = vorbis("bpm")
    bpm: float | None = None
    if bpm_str:
        try:
            bpm = float(bpm_str)
        except ValueError:
            pass

    year, month, day = _parse_date(vorbis("date"))
    track_no = _parse_track_no(vorbis("tracknumber"))

    return TrackTags(
        title=vorbis("title"),
        artist=vorbis("artist"),
        album=vorbis("album"),
        release_year=year,
        release_month=month,
        release_day=day,
        track_no=track_no,
        genre=vorbis("genre"),
        label=vorbis("organization"),
        key=vorbis("initialkey"),
        bpm=bpm,
        recording_mbid=vorbis("musicbrainz_trackid"),
        release_mbid=vorbis("musicbrainz_albumid"),
        earliest_mbid=vorbis("musicbrainz_earliestid"),
    )


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

    def set_text(frame_cls: type, val: str | None) -> None:
        frame_id = frame_cls.__name__
        if val is not None:
            id3[frame_id] = frame_cls(encoding=3, text=[val])
        elif frame_id in id3:
            del id3[frame_id]

    set_text(mutagen.id3.TIT2, tags.title)
    set_text(mutagen.id3.TPE1, tags.artist)
    set_text(mutagen.id3.TALB, tags.album)
    set_text(mutagen.id3.TRCK, str(tags.track_no) if tags.track_no is not None else None)
    set_text(mutagen.id3.TCON, tags.genre)
    set_text(mutagen.id3.TPUB, tags.label)
    set_text(mutagen.id3.TKEY, tags.key)

    date_str = _format_date(tags.release_year, tags.release_month, tags.release_day)
    set_text(mutagen.id3.TDRC, date_str)

    if tags.bpm is not None:
        id3["TBPM"] = mutagen.id3.TBPM(encoding=3, text=[str(int(tags.bpm))])
    elif "TBPM" in id3:
        del id3["TBPM"]

    # UFID for recording_mbid
    ufid_owner = "http://musicbrainz.org"
    if tags.recording_mbid is not None:
        id3[f"UFID:{ufid_owner}"] = mutagen.id3.UFID(
            owner=ufid_owner, data=tags.recording_mbid.encode("utf-8")
        )
    elif f"UFID:{ufid_owner}" in id3:
        del id3[f"UFID:{ufid_owner}"]

    def set_txxx(desc: str, val: str | None) -> None:
        key = f"TXXX:{desc}"
        if val is not None:
            id3[key] = mutagen.id3.TXXX(encoding=3, desc=desc, text=[val])
        elif key in id3:
            del id3[key]

    set_txxx("MusicBrainz Album Id", tags.release_mbid)
    set_txxx("MusicBrainz Earliest Id", tags.earliest_mbid)

    audio.save()


def _write_flac(path: Path, tags: TrackTags) -> None:
    try:
        audio = mutagen.flac.FLAC(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc

    if audio.tags is None:
        audio.add_tags()
    vc = audio.tags
    assert vc is not None

    def set_vorbis(key: str, val: str | None) -> None:
        if val is not None:
            vc[key] = [val]
        elif key in vc:
            del vc[key]

    set_vorbis("title", tags.title)
    set_vorbis("artist", tags.artist)
    set_vorbis("album", tags.album)
    set_vorbis("tracknumber", str(tags.track_no) if tags.track_no is not None else None)
    set_vorbis("genre", tags.genre)
    set_vorbis("organization", tags.label)
    set_vorbis("initialkey", tags.key)
    set_vorbis("date", _format_date(tags.release_year, tags.release_month, tags.release_day))
    set_vorbis("bpm", str(int(tags.bpm)) if tags.bpm is not None else None)
    set_vorbis("musicbrainz_trackid", tags.recording_mbid)
    set_vorbis("musicbrainz_albumid", tags.release_mbid)
    set_vorbis("musicbrainz_earliestid", tags.earliest_mbid)

    audio.save()
