"""Tests for crater.config — the AcoustID key is optional, sources are opt-in."""

from __future__ import annotations

from pathlib import Path

import pytest

from crater import config as cfg_mod
from crater.tags.enrich import (
    KNOWN_SOURCES,
    unknown_field_names,
    unknown_source_names,
)
from crater.tags.errors import ConfigError


@pytest.fixture()
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated XDG_CONFIG_HOME so no real config file is read or scaffolded."""
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.delenv("ACOUSTID_KEY", raising=False)
    return root


def _write_config(xdg: Path, body: str) -> None:
    path = xdg / "crater" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


class TestAcoustidKey:
    def test_absent_key_is_none(self, xdg: Path) -> None:
        assert cfg_mod.tags_acoustid_key(cfg_mod.load()) is None

    def test_config_file_key_is_returned(self, xdg: Path) -> None:
        _write_config(xdg, '[tags]\nacoustid_key = "from-file"\n')
        assert cfg_mod.tags_acoustid_key(cfg_mod.load()) == "from-file"

    def test_env_overrides_config_file(
        self, xdg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(xdg, '[tags]\nacoustid_key = "from-file"\n')
        monkeypatch.setenv("ACOUSTID_KEY", "from-env")
        assert cfg_mod.tags_acoustid_key(cfg_mod.load()) == "from-env"


class TestTagsSources:
    def test_absent_table_is_empty(self, xdg: Path) -> None:
        assert cfg_mod.tags_sources(cfg_mod.load()) == {}

    def test_lists_become_tuples(self, xdg: Path) -> None:
        _write_config(
            xdg, '[tags.sources]\ngenre = ["itunes", "musicbrainz"]\nbpm = []\n'
        )
        assert cfg_mod.tags_sources(cfg_mod.load()) == {
            "genre": ("itunes", "musicbrainz"),
            "bpm": (),
        }

    def test_string_instead_of_list_is_an_error(self, xdg: Path) -> None:
        _write_config(xdg, '[tags.sources]\ngenre = "itunes"\n')
        with pytest.raises(ConfigError):
            cfg_mod.tags_sources(cfg_mod.load())

    def test_non_table_is_an_error(self, xdg: Path) -> None:
        _write_config(xdg, '[tags]\nsources = "deezer"\n')
        with pytest.raises(ConfigError):
            cfg_mod.tags_sources(cfg_mod.load())


class TestArtSources:
    def test_default_order_puts_the_cover_art_archive_first(self, xdg: Path) -> None:
        assert cfg_mod.tags_art_sources(cfg_mod.load()) == (
            "coverartarchive",
            "deezer",
            "itunes",
        )

    def test_override_replaces_the_order(self, xdg: Path) -> None:
        _write_config(xdg, '[tags.sources]\nalbum_art = ["deezer", "itunes"]\n')
        assert cfg_mod.tags_art_sources(cfg_mod.load()) == ("deezer", "itunes")


class TestUnknownSourceNames:
    def test_unknown_name_is_flagged(self) -> None:
        field_sources = {"genre": ("beatport", "deezer"), "bpm": ("beatport",)}
        assert unknown_source_names(field_sources, KNOWN_SOURCES) == ["beatport"]

    def test_known_names_pass(self) -> None:
        field_sources = {"genre": ("deezer", "itunes", "musicbrainz")}
        assert unknown_source_names(field_sources, KNOWN_SOURCES) == []

    def test_the_art_field_and_its_sources_are_known(self) -> None:
        field_sources = {"album_art": cfg_mod.DEFAULT_ART_SOURCES}
        assert unknown_field_names(field_sources) == []
        assert unknown_source_names(field_sources, KNOWN_SOURCES) == []
