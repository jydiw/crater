import os
import sys
import tomllib
from pathlib import Path

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
prefer = "earliest"              # "earliest" | "acquired"
preserve_if_set = ["bpm"]        # never propose overwriting these if already tagged
# acoustid_key = "YOUR_KEY"      # https://acoustid.org/login
                                 # ACOUSTID_KEY env var overrides this
"""

_DEFAULT_PRESERVE_IF_SET: tuple[str, ...] = ("bpm",)


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


def require_acoustid_key(cfg: dict[str, object]) -> str:
    tags = cfg.get("tags", {})
    assert isinstance(tags, dict)
    key = tags.get("acoustid_key")
    if not key:
        raise ConfigError(
            "acoustid_key not set. Use ACOUSTID_KEY env var or set "
            "tags.acoustid_key in ~/.config/crater/config.toml"
        )
    return str(key)


def tags_prefer(cfg: dict[str, object]) -> str:
    tags = cfg.get("tags", {})
    assert isinstance(tags, dict)
    return str(tags.get("prefer", "earliest"))


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
