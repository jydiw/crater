"""Keyless metadata source adapters: Deezer, iTunes, MusicBrainz genres.

Every adapter is lazy and guarded. Lazy: it issues only the requests the merge
engine actually asked for, so a track whose bpm is already tagged costs no
Deezer track call. Guarded: a candidate is believed only when its artist credit
overlaps, its title matches once mix descriptors are dropped, and the duration
doesn't contradict, because these search APIs happily hand back a cover or a
remix for a near-miss query.

Responses are cached in `source_cache` under their full URL and every request
goes through the shared rate limiter. A source that fails — network down,
malformed JSON, no believable hit — proposes nothing; it never breaks a diff.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from crater.db.repo import SourceCacheRepo
from crater.tags.match import artist_overlaps, duration_close, titles_match
from crater.tags.merge import StaticSource, TrackContext
from crater.tags.models import TAGGABLE_FIELDS
from crater.tags.ratelimit import RateLimiter

USER_AGENT = "crater/0.1.0 (https://github.com/jydiw/crater)"

DEEZER_RPS = 10.0
ITUNES_RPS = 0.3
CAA_RPS = 1.0
MB_RPS = 1.0

KNOWN_SOURCES = frozenset({"musicbrainz", "deezer", "itunes", "coverartarchive"})

# Configurable like a tag field in `[tags.sources]`, but never written to a tag.
ART_FIELD = "album_art"

Fetcher = Callable[..., object | None]

GuardMode = Literal["strict", "art"]


def get_json(
    url: str,
    *,
    limiter: RateLimiter,
    bucket: str,
    rps: float,
    timeout: float = 10.0,
) -> object | None:
    """GET and decode JSON. Any failure at all is None: enrichment is optional."""
    limiter.acquire(bucket, rps)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Total JSON accessors — a surprising payload shape must not raise
# ---------------------------------------------------------------------------


def _nested(node: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _entries(payload: object, *keys: str) -> list[dict[str, Any]]:
    node = _nested(payload, *keys)
    if not isinstance(node, list):
        return []
    return [e for e in node if isinstance(e, dict)]


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _guard_ok(
    ctx: TrackContext,
    artist: object,
    title: object,
    duration_s: float | None,
    *,
    mode: GuardMode = "strict",
) -> bool:
    """`duration_close` is None when either side is unknown — not a veto.

    `art` mode drops the duration test altogether: an extended mix ships with
    the single's artwork, so the radio edit's runtime says nothing about
    whether the cover is the right one.
    """
    if not isinstance(artist, str) or not isinstance(title, str):
        return False
    if not artist_overlaps(artist, ctx.before.get("artist") or ""):
        return False
    if not titles_match(title, ctx.before.get("title") or ""):
        return False
    if mode == "art":
        return True
    return duration_close(ctx.duration_s, duration_s) is not False


def _cached_json(
    cache: SourceCacheRepo,
    fetch: Fetcher,
    url: str,
    *,
    limiter: RateLimiter,
    bucket: str,
    rps: float,
) -> object | None:
    """Cache-first GET keyed by the full URL. `bucket` names both the rate-limit
    bucket and the cache's source column."""
    payload = cache.get(bucket, url)
    if payload is not None:
        return payload
    payload = fetch(url, limiter=limiter, bucket=bucket, rps=rps)
    if payload is None:
        return None
    cache.put(bucket, url, payload)
    return payload


def _deezer_search(
    cache: SourceCacheRepo,
    fetch: Fetcher,
    ctx: TrackContext,
    *,
    limiter: RateLimiter,
    bucket: str,
    mode: GuardMode,
) -> dict[str, Any] | None:
    """The first Deezer hit this guard mode believes.

    The tag source and the art source share this so the art lookup lands on the
    already-cached search URL instead of issuing its own request.
    """
    artist = ctx.before.get("artist")
    title = ctx.before.get("title")
    if not artist or not title:
        return None
    query = urllib.parse.urlencode({"q": f'artist:"{artist}" track:"{title}"'})
    payload = _cached_json(
        cache,
        fetch,
        f"https://api.deezer.com/search?{query}",
        limiter=limiter,
        bucket=bucket,
        rps=DEEZER_RPS,
    )
    for candidate in _entries(payload, "data"):
        if _guard_ok(
            ctx,
            _nested(candidate, "artist", "name"),
            candidate.get("title"),
            _num(candidate.get("duration")),
            mode=mode,
        ):
            return candidate
    return None


def _itunes_search(
    cache: SourceCacheRepo,
    fetch: Fetcher,
    ctx: TrackContext,
    *,
    limiter: RateLimiter,
    bucket: str,
    mode: GuardMode,
) -> dict[str, Any] | None:
    """The first iTunes result this guard mode believes. Shared like the Deezer
    one, and for the same reason."""
    artist = ctx.before.get("artist")
    title = ctx.before.get("title")
    if not artist or not title:
        return None
    query = urllib.parse.urlencode(
        {"term": f"{artist} {title}", "entity": "song", "limit": 10}
    )
    payload = _cached_json(
        cache,
        fetch,
        f"https://itunes.apple.com/search?{query}",
        limiter=limiter,
        bucket=bucket,
        rps=ITUNES_RPS,
    )
    for result in _entries(payload, "results"):
        ms = _num(result.get("trackTimeMillis"))
        if _guard_ok(
            ctx,
            result.get("artistName"),
            result.get("trackName"),
            ms / 1000 if ms is not None else None,
            mode=mode,
        ):
            return result
    return None


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


@dataclass
class DeezerSource:
    """Deezer search → bpm (from /track) and genre (from /album)."""

    cache: SourceCacheRepo
    limiter: RateLimiter
    fetch: Fetcher = get_json
    name: str = "deezer"

    def propose(
        self, ctx: TrackContext, fields: frozenset[str]
    ) -> dict[str, str | None]:
        wanted = fields & {"bpm", "genre"}
        if not wanted:
            return {}
        hit = self._search(ctx)
        if hit is None:
            return {}

        proposed: dict[str, str | None] = {}
        if "bpm" in wanted:
            bpm = self._bpm(hit, ctx)
            if bpm is not None:
                proposed["bpm"] = bpm
        if "genre" in wanted:
            genre = self._genre(hit)
            if genre is not None:
                proposed["genre"] = genre
        return proposed

    def _get(self, url: str) -> object | None:
        return _cached_json(
            self.cache,
            self.fetch,
            url,
            limiter=self.limiter,
            bucket=self.name,
            rps=DEEZER_RPS,
        )

    def _search(self, ctx: TrackContext) -> dict[str, Any] | None:
        return _deezer_search(
            self.cache,
            self.fetch,
            ctx,
            limiter=self.limiter,
            bucket=self.name,
            mode="strict",
        )

    def _bpm(self, hit: dict[str, Any], ctx: TrackContext) -> str | None:
        track_id = hit.get("id")
        if not track_id:
            return None
        track = self._get(f"https://api.deezer.com/track/{track_id}")
        bpm = _num(_nested(track, "bpm"))
        if not bpm:  # 0.0 is Deezer's "we don't know"
            return None
        if bpm != round(bpm):
            ctx.notes.append(f"deezer bpm {bpm}")
        return str(round(bpm))

    def _genre(self, hit: dict[str, Any]) -> str | None:
        album_id = _nested(hit, "album", "id")
        if not album_id:
            return None
        album = self._get(f"https://api.deezer.com/album/{album_id}")
        for entry in _entries(album, "genres", "data"):
            name = _text(entry.get("name"))
            if name is not None:
                return name
        return None


@dataclass
class ITunesSource:
    """iTunes Search → genre. One request per track, whatever else is asked."""

    cache: SourceCacheRepo
    limiter: RateLimiter
    fetch: Fetcher = get_json
    name: str = "itunes"

    def propose(
        self, ctx: TrackContext, fields: frozenset[str]
    ) -> dict[str, str | None]:
        if "genre" not in fields:
            return {}
        hit = _itunes_search(
            self.cache,
            self.fetch,
            ctx,
            limiter=self.limiter,
            bucket=self.name,
            mode="strict",
        )
        if hit is None:
            return {}
        genre = _text(hit.get("primaryGenreName"))
        return {"genre": genre} if genre is not None else {}


# ---------------------------------------------------------------------------
# Album art sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtCandidate:
    """One source's answer for cover art. `key` becomes the staged file's stem,
    so it has to stay filesystem-safe."""

    source: str
    key: str
    url: str


class ArtSource(Protocol):
    name: str

    def find(self, ctx: TrackContext) -> ArtCandidate | None: ...


@dataclass
class DeezerArtSource:
    """Deezer's album cover — the single's artwork, for promo tracks MB and the
    Cover Art Archive have never heard of."""

    cache: SourceCacheRepo
    limiter: RateLimiter
    fetch: Fetcher = get_json
    name: str = "deezer"

    def find(self, ctx: TrackContext) -> ArtCandidate | None:
        hit = _deezer_search(
            self.cache,
            self.fetch,
            ctx,
            limiter=self.limiter,
            bucket=self.name,
            mode="art",
        )
        if hit is None:
            return None
        album_id = _nested(hit, "album", "id")
        if not album_id:
            return None
        for size in ("cover_xl", "cover_big", "cover_medium"):
            url = _text(_nested(hit, "album", size))
            if url is not None:
                return ArtCandidate(self.name, f"deezer-{album_id}", url)
        return None


@dataclass
class ITunesArtSource:
    """iTunes cover art. The search only ever returns the 100px thumbnail, but
    the dimensions live in the URL and any size can be asked for."""

    cache: SourceCacheRepo
    limiter: RateLimiter
    fetch: Fetcher = get_json
    name: str = "itunes"

    def find(self, ctx: TrackContext) -> ArtCandidate | None:
        hit = _itunes_search(
            self.cache,
            self.fetch,
            ctx,
            limiter=self.limiter,
            bucket=self.name,
            mode="art",
        )
        if hit is None:
            return None
        collection_id = hit.get("collectionId")
        url = _text(hit.get("artworkUrl100"))
        if not collection_id or url is None:
            return None
        return ArtCandidate(
            self.name,
            f"itunes-{collection_id}",
            url.replace("100x100bb", "1200x1200bb"),
        )


@dataclass
class MbGenreFetcher:
    """Recording genres over MB's web service — the one MB field the
    musicbrainzngs recording lookup doesn't already carry."""

    cache: SourceCacheRepo
    limiter: RateLimiter
    fetch: Fetcher = get_json

    def genre(self, recording_mbid: str) -> str | None:
        """The most-voted genre for this recording; ties keep MB's own order."""
        payload = _cached_json(
            self.cache,
            self.fetch,
            f"https://musicbrainz.org/ws/2/recording/{recording_mbid}"
            "?inc=genres&fmt=json",
            limiter=self.limiter,
            bucket="musicbrainz",
            rps=MB_RPS,
        )
        best: tuple[float, str] | None = None
        for entry in _entries(payload, "genres"):
            name = _text(entry.get("name"))
            count = _num(entry.get("count")) or 0.0
            if name is not None and (best is None or count > best[0]):
                best = (count, name)
        return best[1] if best is not None else None


@dataclass
class MbSource:
    """The already-fetched MB payload's fields, plus a genre call on demand."""

    static: StaticSource
    genre_fetch: MbGenreFetcher | None = None
    name: str = "musicbrainz"

    def propose(
        self, ctx: TrackContext, fields: frozenset[str]
    ) -> dict[str, str | None]:
        proposed = self.static.propose(ctx, fields)
        if (
            "genre" in fields
            and not proposed.get("genre")
            and ctx.recording_mbid
            and self.genre_fetch is not None
        ):
            genre = self.genre_fetch.genre(ctx.recording_mbid)
            if genre is not None:
                proposed["genre"] = genre
        return proposed


# ---------------------------------------------------------------------------
# Config validation helpers
# ---------------------------------------------------------------------------


def unknown_source_names(
    field_sources: dict[str, tuple[str, ...]], known: frozenset[str]
) -> list[str]:
    """Source names the config asks for that crater has no adapter for."""
    return sorted(
        {n for names in field_sources.values() for n in names if n not in known}
    )


def unknown_field_names(field_sources: dict[str, tuple[str, ...]]) -> list[str]:
    known = set(TAGGABLE_FIELDS) | {ART_FIELD}
    return sorted(f for f in field_sources if f not in known)
