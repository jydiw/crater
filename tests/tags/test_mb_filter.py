"""Pure-function tests for crater/tags/mb_filter.py."""

from __future__ import annotations

import pytest

from crater.tags.mb_filter import (
    artist_credit_join,
    earliest_release,
    parse_partial_date,
    payload_to_tags,
)


class TestParsePartialDate:
    @pytest.mark.parametrize(
        "s,expected",
        [
            ("2020-01-15", (2020, 1, 15)),
            ("2020-01", (2020, 1, None)),
            ("2020", (2020, None, None)),
            ("", (None, None, None)),
            (None, (None, None, None)),
            ("not-a-date", (None, None, None)),
        ],
    )
    def test_shapes(self, s: str | None, expected: tuple) -> None:
        assert parse_partial_date(s) == expected


class TestArtistCreditJoin:
    def test_single_artist(self) -> None:
        credit = [{"name": "Autechre"}]
        assert artist_credit_join(credit) == "Autechre"

    def test_join_phrase_on_dict(self) -> None:
        credit = [
            {"name": "Aphex Twin", "joinphrase": " & "},
            {"name": "Luke Vibert"},
        ]
        assert artist_credit_join(credit) == "Aphex Twin & Luke Vibert"

    def test_string_join_items_passthrough(self) -> None:
        credit = [{"name": "A"}, " feat. ", {"name": "B"}]
        assert artist_credit_join(credit) == "A feat. B"

    def test_falls_back_to_artist_name(self) -> None:
        credit = [{"artist": {"name": "Foo"}}]
        assert artist_credit_join(credit) == "Foo"


def _release(id_: str, date: str | None, title: str = "Album") -> dict:
    return {"id": id_, "title": title, "date": date, "status": "Official"}


def _rel(
    id_: str,
    date: str | None = "2005-01-01",
    *,
    status: str | None = "Official",
    primary: str | None = "Single",
    secondary: tuple[str, ...] = (),
    catno: str | None = None,
) -> dict:
    """One Release as MusicBrainz's JSON browse returns it."""
    release: dict = {
        "id": id_,
        "title": f"Album {id_}",
        "date": date,
        "release-group": {
            "id": f"rg-{id_}",
            "primary-type": primary,
            "secondary-types": list(secondary),
        },
        "label-info": [{"catalog-number": catno, "label": {"name": "Label"}}],
    }
    if status is not None:
        release["status"] = status
    return release


def _earliest_id(*releases: dict) -> str | None:
    picked = earliest_release({"releases": list(releases)})
    return picked["id"] if picked is not None else None


class TestEarliestReleaseRanking:
    """The Earliest release rules, one case per line."""

    @pytest.mark.parametrize("status", ["Promotion", "Bootleg", None])
    def test_a_release_that_is_not_official_is_never_the_earliest(
        self, status: str | None
    ) -> None:
        unofficial = _rel("unofficial", "2001-01-01", status=status)
        official = _rel("official", "2008-01-01")
        assert _earliest_id(unofficial, official) == "official"

    @pytest.mark.parametrize(
        "primary,secondary",
        [
            ("Album", ("Compilation",)),
            ("Album", ("DJ-mix",)),
            ("Album", ("Compilation", "DJ-mix")),
            ("Other", ()),
            ("Broadcast", ()),
            (None, ()),
        ],
    )
    def test_a_compilation_dj_mix_or_other_type_loses_to_a_later_single(
        self, primary: str | None, secondary: tuple[str, ...]
    ) -> None:
        sidelined = _rel(
            "sidelined", "2001-01-01", primary=primary, secondary=secondary
        )
        single = _rel("single", "2008-01-01", primary="Single")
        assert _earliest_id(sidelined, single) == "single"

    @pytest.mark.parametrize("primary", ["Single", "EP", "Album"])
    def test_single_ep_and_album_rank_on_equal_terms(self, primary: str) -> None:
        early = _rel("early", "2001-01-01", primary=primary)
        late = _rel("late", "2008-01-01", primary="Single")
        assert _earliest_id(early, late) == "early"

    def test_a_release_with_another_secondary_type_still_counts(self) -> None:
        remixes = _rel("remixes", "2001-01-01", primary="EP", secondary=("Remix",))
        single = _rel("single", "2008-01-01")
        assert _earliest_id(remixes, single) == "remixes"

    def test_a_compilation_is_chosen_when_nothing_else_is_official(self) -> None:
        promo = _rel("promo", "2000-01-01", status="Promotion")
        late_comp = _rel("late-comp", "2011-01-01", secondary=("Compilation",))
        early_mix = _rel("early-mix", "2009-01-01", secondary=("DJ-mix",))
        assert _earliest_id(promo, late_comp, early_mix) == "early-mix"

    @pytest.mark.parametrize(
        "earlier,later",
        [
            ("2009-06-08", "2009"),
            ("2009-06-08", "2009-06"),
            ("2009-06", "2009-07-01"),
            ("2009-12-31", "2009"),
            ("2009", "2010-01-01"),
            ("2010", None),
            ("2010", ""),
        ],
    )
    def test_a_missing_month_or_day_ranks_after_every_known_one(
        self, earlier: str, later: str | None
    ) -> None:
        assert _earliest_id(_rel("later", later), _rel("earlier", earlier)) == "earlier"
        assert _earliest_id(_rel("earlier", earlier), _rel("later", later)) == "earlier"

    @pytest.mark.parametrize(
        "with_catno,without_catno",
        [
            (_rel("b", catno="ANJ-014"), _rel("a", catno=None)),
            (_rel("b", catno="ANJ-014"), _rel("a", catno="")),
            (_rel("b", catno="ANJ-014"), _rel("a", catno="[none]")),
            (_rel("b", catno="ANJ-014"), {**_rel("a"), "label-info": []}),
        ],
    )
    def test_a_tie_goes_to_the_release_with_a_catalog_number(
        self, with_catno: dict, without_catno: dict
    ) -> None:
        assert _earliest_id(without_catno, with_catno) == "b"
        assert _earliest_id(with_catno, without_catno) == "b"

    @pytest.mark.parametrize("catno", [None, "ANJ-014"])
    def test_a_remaining_tie_goes_to_the_lower_id(self, catno: str | None) -> None:
        a, b, c = (_rel(i, catno=catno) for i in ("a", "b", "c"))
        assert _earliest_id(c, a, b) == "a"
        assert _earliest_id(b, c, a) == "a"

    @pytest.mark.parametrize("status", ["Promotion", "Bootleg", None])
    def test_no_official_release_means_no_earliest(self, status: str | None) -> None:
        assert _earliest_id(_rel("r", status=status)) is None


class TestEarliestRelease:
    def test_picks_earliest_by_date(self) -> None:
        payload = {
            "releases": [
                _release("r1", "2010-05-01"),
                _release("r2", "2005-03-20"),
                _release("r3", "2008-01"),
            ]
        }
        assert earliest_release(payload)["id"] == "r2"

    def test_missing_date_sorts_last(self) -> None:
        payload = {
            "releases": [
                _release("r1", None),
                _release("r2", "2020-01-01"),
            ]
        }
        assert earliest_release(payload)["id"] == "r2"

    def test_empty_returns_none(self) -> None:
        assert earliest_release({"releases": []}) is None


class TestPayloadToTags:
    def test_full_mapping(self) -> None:
        payload = {
            "id": "rec-1",
            "title": "Song",
            "artist-credit": [{"name": "Artist"}],
        }
        release = {
            "id": "rel-1",
            "title": "Album",
            "date": "1996-09-23",
            "label-info": [{"catalog-number": "WARPCD43", "label": {"name": "Warp"}}],
        }
        tags = payload_to_tags(payload, release, earliest_mbid="rel-0")
        assert tags == {
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "release_year": "1996",
            "release_month": "9",
            "release_day": "23",
            "label": "Warp",
            "recording_mbid": "rec-1",
            "release_mbid": "rel-1",
            "earliest_mbid": "rel-0",
        }

    def test_partial_date_nullifies_missing_parts(self) -> None:
        payload = {"id": "x", "title": "T", "artist-credit": [{"name": "A"}]}
        release = {"id": "r", "title": "Alb", "date": "2020"}
        tags = payload_to_tags(payload, release)
        assert tags["release_year"] == "2020"
        assert tags["release_month"] is None
        assert tags["release_day"] is None

    @pytest.mark.parametrize(
        "label_info,expected",
        [
            (
                [
                    {"catalog-number": None, "label": {"name": "Distributor"}},
                    {"catalog-number": "ANJ-014", "label": {"name": "Anjunabeats"}},
                ],
                "Anjunabeats",
            ),
            (
                [
                    {"catalog-number": "[none]", "label": {"name": "First"}},
                    {"catalog-number": "X-1", "label": {"name": "Second"}},
                ],
                "Second",
            ),
            (
                [
                    {"catalog-number": "X-1", "label": None},
                    {"catalog-number": None, "label": {"name": "Named"}},
                ],
                "Named",
            ),
            ([{"catalog-number": None, "label": {"name": "Only"}}], "Only"),
            ([], None),
        ],
    )
    def test_label_comes_from_the_entry_with_a_catalog_number(
        self, label_info: list, expected: str | None
    ) -> None:
        payload = {"id": "x", "title": "T", "artist-credit": [{"name": "A"}]}
        release = {"id": "r", "title": "Alb", "date": "2020", "label-info": label_info}
        assert payload_to_tags(payload, release)["label"] == expected

    def test_missing_label_is_none(self) -> None:
        payload = {"id": "x", "title": "T", "artist-credit": [{"name": "A"}]}
        release = {"id": "r", "title": "Alb", "date": "2020"}
        assert payload_to_tags(payload, release)["label"] is None

    def test_does_not_emit_bpm_genre_key(self) -> None:
        payload = {"id": "x", "title": "T", "artist-credit": [{"name": "A"}]}
        release = {"id": "r", "title": "Alb", "date": "2020"}
        tags = payload_to_tags(payload, release)
        for f in ("bpm", "genre", "key", "track_no"):
            assert f not in tags
