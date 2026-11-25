"""Tests for crater.tags.match — normalization and text-search acceptance."""

from __future__ import annotations

import pytest

from crater.tags.match import (
    artist_overlaps,
    duration_close,
    house_style_title,
    normalize,
    strip_mix_suffix,
    text_match_ok,
    titles_match,
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Aphex Twin", "aphex twin"),
            ("APHEX TWIN", "aphex twin"),
            ("Don't Stop", "dont stop"),
            ("Windowlicker (Remix)", "windowlicker remix"),
            ("Four  Tet", "four tet"),
            ("  Spaced \t Out\n", "spaced out"),
            ("", ""),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert normalize(raw) == expected


class TestDurationClose:
    @pytest.mark.parametrize(
        "a,b,expected",
        [
            (200.0, 200.0, True),
            (200.0, 201.5, True),
            (200.0, 203.0, True),
            (203.0, 200.0, True),
            (200.0, 210.0, False),
            (None, 200.0, None),
            (200.0, None, None),
            (None, None, None),
        ],
    )
    def test_duration_close(
        self, a: float | None, b: float | None, expected: bool | None
    ) -> None:
        assert duration_close(a, b) is expected

    def test_tolerance_is_configurable(self) -> None:
        assert duration_close(200.0, 208.0, tolerance_s=10.0) is True


class TestTextMatchOk:
    @pytest.mark.parametrize(
        "score,artist_eq,title_eq,dur,expected",
        [
            # duration corroborates: score alone decides
            (0.95, True, True, True, True),
            (0.90, False, False, True, True),
            (0.89, True, True, True, False),
            # duration unknown: needs a near-perfect score and exact strings
            (0.95, True, True, None, True),
            (0.94, True, True, None, False),
            (0.95, False, True, None, False),
            (0.95, True, False, None, False),
            # duration disagrees: nothing else matters
            (1.0, True, True, False, False),
        ],
    )
    def test_text_match_ok(
        self,
        score: float,
        artist_eq: bool,
        title_eq: bool,
        dur: bool | None,
        expected: bool,
    ) -> None:
        assert text_match_ok(score, artist_eq, title_eq, dur) is expected


class TestStripMixSuffix:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Come Alive (Extended Mix)", "Come Alive"),
            ("Cola (Original Mix)", "Cola"),
            ("Sun & Moon (Radio Edit)", "Sun & Moon"),
            ("Foo [Extended]", "Foo"),
            ("Foo (extended mix)", "Foo"),
            ("Foo (Radio Version)", "Foo"),
        ],
    )
    def test_descriptors_are_stripped(self, raw: str, expected: str) -> None:
        assert strip_mix_suffix(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Aria (Argy Remix)",
            "Cola (Dario D'Attis Remix)",
            "X (Mousse T.'s Glitterbox Mix)",
            "X (Instrumental)",
            "Song (Live at Wembley)",
            "Plain Title",
            "Foo (Extended Mix) bar",
        ],
    )
    def test_everything_else_survives(self, raw: str) -> None:
        assert strip_mix_suffix(raw) == raw


class TestTitlesMatch:
    def test_a_remix_is_a_different_recording(self) -> None:
        assert titles_match("Aria", "Aria (Argy Remix)") is False

    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("Come Alive", "Come Alive (Extended Mix)", True),
            ("Come Alive (Extended Mix)", "Come Alive (Radio Edit)", True),
            ("Come Alive", "Sun & Moon", False),
            ("", "Come Alive", False),
            ("Come Alive", "", False),
            ("", "", False),
        ],
    )
    def test_titles_match(self, a: str, b: str, expected: bool) -> None:
        assert titles_match(a, b) is expected


class TestArtistOverlaps:
    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("Jerome Isma-Ae", "Jerome Isma-Ae, JES", True),
            ("Jerome Isma-Ae, JES", "Jerome Isma-Ae", True),
            ("Above & Beyond", "Above & Beyond", True),
            ("Above & Beyond", "Armin van Buuren", False),
            ("", "Armin van Buuren", False),
            ("Armin van Buuren", "", False),
        ],
    )
    def test_artist_overlaps(self, a: str, b: str, expected: bool) -> None:
        assert artist_overlaps(a, b) is expected


class TestHouseStyleTitle:
    @pytest.mark.parametrize(
        "source,current,expected",
        [
            (
                "Sky Falls Down (Armin van Buuren remix)",
                None,
                "Sky Falls Down (Armin van Buuren Remix)",
            ),
            ("Sun & Moon (radio edit)", None, "Sun & Moon (Radio Edit)"),
            ("Come Alive (extended mix)", None, "Come Alive (Extended Mix)"),
            ("Come Alive [extended mix]", None, "Come Alive [Extended Mix]"),
            ("Foo [radio edit]", None, "Foo [Radio Edit]"),
            ("X (dub)", None, "X (Dub)"),
            ("X (vocal version)", None, "X (Vocal Version)"),
            ("X (instrumental)", None, "X (Instrumental)"),
            ("X (Argy rework)", None, "X (Argy Rework)"),
            ("X (Argy bootleg)", None, "X (Argy Bootleg)"),
            ("X (original mix)", None, "X (Original Mix)"),
            ("X (club mix)", None, "X (Club Mix)"),
            ("X (REMIX)", None, "X (REMIX)"),
            ("X (rEMIX)", None, "X (REMIX)"),
            ("club life", None, "club life"),
            ("radio edit (Argy remix)", None, "radio edit (Argy Remix)"),
            ("Song (feat. someone)", None, "Song (feat. someone)"),
            ("Song [feat. someone]", None, "Song [feat. someone]"),
            ("Song (the remixes)", None, "Song (the remixes)"),
            ("Song (mixed by someone)", None, "Song (mixed by someone)"),
            ("Song (dubstep edit)", None, "Song (dubstep Edit)"),
            ("Song (feat. van dyke) (dub)", None, "Song (feat. van dyke) (Dub)"),
            ("Cola", "Cola (Original Mix)", "Cola (Original Mix)"),
            ("Cola", "cola (original mix)", "Cola (Original Mix)"),
            ("Cola", "Cola [ORIGINAL MIX]  ", "Cola (Original Mix)"),
            ("Cola (feat. X)", "Cola (Original Mix)", "Cola (feat. X) (Original Mix)"),
            ("Cola", "Cola", "Cola"),
            ("Cola", None, "Cola"),
            ("Cola", "Cola (Extended Mix)", "Cola"),
            ("Cola", "Cola (Original Mix) bar", "Cola"),
            ("Cola (extended mix)", "Cola (Original Mix)", "Cola (Extended Mix)"),
            ("Cola (Argy remix)", "Cola (Original Mix)", "Cola (Argy Remix)"),
            ("Cola (re-edit)", None, "Cola (Re-Edit)"),
            ("Cola (someone re-edit)", None, "Cola (someone Re-Edit)"),
            ("Cola (Re-edit)", None, "Cola (Re-Edit)"),
            ("Cola (Re-Edit)", None, "Cola (Re-Edit)"),
            ("Cola [re-edit]", None, "Cola [Re-Edit]"),
            ("Cola (radio-edit)", None, "Cola (Radio-Edit)"),
            ("Cola (non-vocal mix)", None, "Cola (Non-Vocal Mix)"),
            ("Cola (12-inch mix)", None, "Cola (12-inch Mix)"),
            ("Cola (lo-fi version)", None, "Cola (lo-fi Version)"),
            ("re-edit Cola", None, "re-edit Cola"),
            ("Cola (re-edit)", "Cola (Original Mix)", "Cola (Re-Edit)"),
        ],
    )
    def test_house_style_title(
        self, source: str, current: str | None, expected: str
    ) -> None:
        assert house_style_title(source, current) == expected
