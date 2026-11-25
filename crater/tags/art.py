"""Album art: fetch from an ordered list of sources, embed, strip, read back.

Sources are tried in configured order — the Cover Art Archive first, then
Deezer and iTunes, which carry the promo and DJ-pack singles MB has no release
for — and the first one that hands back sniffable bytes wins. Art is staged to
a file at diff time so the user can look at the image before approving the CSV;
apply embeds the staged bytes, revert either strips the picture or re-embeds
the bytes it stashed. Nothing here decodes an image — the magic bytes decide
the format, because an embedded picture's declared mime routinely lies.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import mutagen.flac
import mutagen.id3
import mutagen.mp3

from crater.tags.enrich import (
    CAA_RPS,
    DEEZER_RPS,
    ITUNES_RPS,
    USER_AGENT,
    ArtCandidate,
    ArtSource,
)
from crater.tags.errors import TagIOError
from crater.tags.io import detect_format
from crater.tags.merge import TrackContext
from crater.tags.ratelimit import RateLimiter

CAA_RELEASE = "https://coverartarchive.org/release"
CAA_SOURCE = "coverartarchive"

# Rate bucket and rps per art source. CAA's bucket name predates the source name.
_ART_LIMITS: dict[str, tuple[str, float]] = {
    CAA_SOURCE: ("caa", CAA_RPS),
    "deezer": ("deezer", DEEZER_RPS),
    "itunes": ("itunes", ITUNES_RPS),
}

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_EXT = {"image/jpeg": ".jpg", "image/png": ".png"}

_FRONT_COVER = 3


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    return None


@dataclass(frozen=True)
class ArtBlob:
    data: bytes
    mime: str


def _open_mp3(path: Path) -> mutagen.mp3.MP3:
    try:
        return mutagen.mp3.MP3(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc


def _open_flac(path: Path) -> mutagen.flac.FLAC:
    try:
        return mutagen.flac.FLAC(path)
    except Exception as exc:
        raise TagIOError(f"could not open {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_art(path: Path) -> ArtBlob | None:
    if detect_format(path) == "mp3":
        return _read_mp3_art(path)
    return _read_flac_art(path)


def _blob(data: bytes, declared: str) -> ArtBlob:
    return ArtBlob(data, sniff_mime(data) or declared)


def _read_mp3_art(path: Path) -> ArtBlob | None:
    tags = _open_mp3(path).tags
    if tags is None:
        return None
    frames = tags.getall("APIC")
    if not frames:
        return None
    return _blob(frames[0].data, frames[0].mime)


def _read_flac_art(path: Path) -> ArtBlob | None:
    pictures = _open_flac(path).pictures
    if not pictures:
        return None
    front = next((p for p in pictures if p.type == _FRONT_COVER), pictures[0])
    return _blob(front.data, front.mime)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def embed_art(path: Path, blob: ArtBlob) -> None:
    if detect_format(path) == "mp3":
        _embed_mp3_art(path, blob)
    else:
        _embed_flac_art(path, blob)


def _embed_mp3_art(path: Path, blob: ArtBlob) -> None:
    audio = _open_mp3(path)
    if audio.tags is None:
        audio.add_tags()
    id3 = audio.tags
    assert id3 is not None
    id3.delall("APIC")
    id3.add(
        mutagen.id3.APIC(
            encoding=3, mime=blob.mime, type=_FRONT_COVER, desc="", data=blob.data
        )
    )
    audio.save()


def _embed_flac_art(path: Path, blob: ArtBlob) -> None:
    audio = _open_flac(path)
    picture = mutagen.flac.Picture()
    picture.type = _FRONT_COVER
    picture.mime = blob.mime
    picture.data = blob.data
    audio.clear_pictures()
    audio.add_picture(picture)
    audio.save()


def strip_art(path: Path) -> None:
    if detect_format(path) == "mp3":
        audio = _open_mp3(path)
        if audio.tags is None:
            return
        audio.tags.delall("APIC")
    else:
        audio = _open_flac(path)
        audio.clear_pictures()
    audio.save()


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_bytes(
    url: str,
    *,
    limiter: RateLimiter,
    bucket: str,
    rps: float,
    timeout: float = 10.0,
) -> bytes | None:
    """GET raw bytes. Any failure at all is None: art is optional."""
    limiter.acquire(bucket, rps)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception:
        return None


@dataclass
class CaaArtSource:
    """The Cover Art Archive front cover for the release MB settled on."""

    name: str = CAA_SOURCE

    def find(self, ctx: TrackContext) -> ArtCandidate | None:
        if not ctx.release_mbid:
            return None
        return ArtCandidate(
            self.name, ctx.release_mbid, f"{CAA_RELEASE}/{ctx.release_mbid}/front-500"
        )


@dataclass(frozen=True)
class StagedArt:
    """A staged image plus the source that supplied it, so the review CSV can
    name where a cover came from."""

    path: Path
    source: str


@dataclass
class ArtStager:
    """Ordered art sources → a file under `art_dir`, one per candidate key.

    A key already on disk is never re-fetched, so re-running a diff over the
    same album costs nothing.
    """

    art_dir: Path
    limiter: RateLimiter
    sources: list[ArtSource] = field(default_factory=list)
    fetch: Callable[..., bytes | None] = fetch_bytes

    def stage(self, ctx: TrackContext) -> StagedArt | None:
        for source in self.sources:
            candidate = source.find(ctx)
            if candidate is None:
                continue

            staged = self._on_disk(candidate.key)
            if staged is not None:
                return StagedArt(staged, candidate.source)

            data = self._get(candidate)
            if data is None:
                continue
            mime = sniff_mime(data)
            if mime is None:  # a 404 page or a redirect, not an image
                continue

            self.art_dir.mkdir(parents=True, exist_ok=True)
            out = self.art_dir / f"{candidate.key}{_EXT[mime]}"
            out.write_bytes(data)
            return StagedArt(out, candidate.source)
        return None

    def _on_disk(self, key: str) -> Path | None:
        for ext in _EXT.values():
            staged = self.art_dir / f"{key}{ext}"
            if staged.exists():
                return staged
        return None

    def _get(self, candidate: ArtCandidate) -> bytes | None:
        bucket, rps = _ART_LIMITS.get(candidate.source, (candidate.source, 1.0))
        data = self.fetch(candidate.url, limiter=self.limiter, bucket=bucket, rps=rps)
        if data is None and candidate.source == CAA_SOURCE:
            # Not every release has the 500px thumbnail; the full front does.
            data = self.fetch(
                candidate.url.replace("front-500", "front"),
                limiter=self.limiter,
                bucket=bucket,
                rps=rps,
            )
        return data
