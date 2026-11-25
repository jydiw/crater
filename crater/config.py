import os
import sys
import tomllib
from pathlib import Path

from crater.tags.enrich import ART_FIELD
from crater.tags.errors import ConfigError


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "crater" / "config.toml"


_CONFIG_TEMPLATE = """\
# crater configuration
# https://github.com/jydiw/crater

# library_root = "/path/to/your/dj/library"
# CRATER_LIBRARY_ROOT env var overrides this

[tags]
preserve_if_set = ["bpm"]          # never propose overwriting these if already tagged
acoustid_skip_threshold = 0.90     # skip fingerprint when stored score ≥ this
acoustid_high_threshold = 0.90     # score ≥ this → decision=accept, confidence=high
acoustid_medium_threshold = 0.75   # score ≥ this → confidence=medium (review)
# acoustid_key = "YOUR_KEY"        # optional — https://acoustid.org/login
                                   # without it, files MB text search can't
                                   # identify are skipped, not fingerprinted
                                   # ACOUSTID_KEY env var overrides this

# [tags.sources]                   # per-field source priority, highest first.
# genre = ["deezer", "itunes", "musicbrainz"]
# bpm = ["deezer"]                 # [] means "never propose it"
"""

_DEFAULT_PRESERVE_IF_SET: tuple[str, ...] = ("bpm",)
# Album art source order, highest first. Overridable as `[tags.sources] album_art`.
DEFAULT_ART_SOURCES: tuple[str, ...] = ("coverartarchive", "deezer", "itunes")
_DEFAULT_SKIP_THRESHOLD = 0.90
_DEFAULT_HIGH_THRESHOLD = 0.90
_DEFAULT_MEDIUM_THRESHOLD = 0.75


def _scaffold_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONFIG_TEMPLATE)
    print(f"created {path}", file=sys.stderr)


def load(library_root: Path | None = None) -> dict[str, object]:
    data: dict[str, object] = {}

    path = _config_path()
    if not path.exists():
        _scaffold_config(path)
    else:
        with path.open("rb") as f:
            data = tomllib.load(f)

    # Env overrides
    if env_root := os.environ.get("CRATER_LIBRARY_ROOT"):
        data["library_root"] = env_root
    if library_root is not None:
        data["library_root"] = str(library_root)

    tags = data.setdefault("tags", {})
    assert isinstance(tags, dict)
    if env_key := os.environ.get("ACOUSTID_KEY"):
        tags["acoustid_key"] = env_key

    return data


def require_library_root(cfg: dict[str, object]) -> Path:
    val = cfg.get("library_root")
    if not val:
        raise ConfigError(
            "library_root not set. Use --library, CRATER_LIBRARY_ROOT env var, "
            "or set library_root in ~/.config/crater/config.toml"
        )
    return Path(str(val)).expanduser()


def tags_acoustid_key(cfg: dict[str, object]) -> str | None:
    """The AcoustID key, or None when none is configured.

    Optional: without a key, files that MB text search can't identify are
    skipped instead of fingerprinted.
    """
    tags = cfg.get("tags", {})
    assert isinstance(tags, dict)
    key = tags.get("acoustid_key")
    return str(key) if key else None


def tags_preserve_if_set(cfg: dict[str, object]) -> list[str]:
    tags = cfg.get("tags", {})
    assert isinstance(tags, dict)
    val = tags.get("preserve_if_set")
    if val is None:
        return list(_DEFAULT_PRESERVE_IF_SET)
    if not isinstance(val, list):
        raise ConfigError(
            "tags.preserve_if_set must be a list of field names, "
            f"got {type(val).__name__}"
        )
    return [str(f) for f in val]


def tags_sources(cfg: dict[str, object]) -> dict[str, tuple[str, ...]]:
    """Per-field source priority from `[tags.sources]`.

    Only the configured overrides — the caller merges them over the defaults.
    """
    tags = cfg.get("tags", {})
    assert isinstance(tags, dict)
    val = tags.get("sources")
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ConfigError(
            "tags.sources must be a table of field = [source, ...], "
            f"got {type(val).__name__}"
        )
    sources: dict[str, tuple[str, ...]] = {}
    for field_name, names in val.items():
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ConfigError(
                f"tags.sources.{field_name} must be a list of source names, "
                f"got {names!r}"
            )
        sources[field_name] = tuple(names)
    return sources


def tags_art_sources(cfg: dict[str, object]) -> tuple[str, ...]:
    """Album art source order from `[tags.sources] album_art`, highest first."""
    return tags_sources(cfg).get(ART_FIELD, DEFAULT_ART_SOURCES)


def _float_from_tags(cfg: dict[str, object], key: str, default: float) -> float:
    tags = cfg.get("tags", {})
    assert isinstance(tags, dict)
    val = tags.get(key, default)
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ConfigError(f"tags.{key} must be a number, got {val!r}") from e


def tags_acoustid_skip(cfg: dict[str, object]) -> float:
    return _float_from_tags(cfg, "acoustid_skip_threshold", _DEFAULT_SKIP_THRESHOLD)


def tags_acoustid_high(cfg: dict[str, object]) -> float:
    return _float_from_tags(cfg, "acoustid_high_threshold", _DEFAULT_HIGH_THRESHOLD)


def tags_acoustid_medium(cfg: dict[str, object]) -> float:
    return _float_from_tags(cfg, "acoustid_medium_threshold", _DEFAULT_MEDIUM_THRESHOLD)
