"""Pure-function tests for crater/tags/mb_filter.py."""

from __future__ import annotations

import pytest

from crater.tags.mb_filter import (
    artist_credit_join,
    earliest_release,
    parse_partial_date,
    payload_to_tags,
    pick_release,
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
    return {"id": id_, "title": title, "date": date}


class TestEarliestRelease:
    def test_picks_earliest_by_date(self) -> None:
        payload = {
            "release-list": [
                _release("r1", "2010-05-01"),
                _release("r2", "2005-03-20"),
                _release("r3", "2008-01"),
            ]
        }
        assert earliest_release(payload)["id"] == "r2"

    def test_year_only_sorts_correctly(self) -> None:
        payload = {
            "release-list": [
                _release("r1", "2001-12-31"),
                _release("r2", "2001"),
            ]
        }
        # 2001 year-only → (2001, 0, 0) sorts before 2001-12-31
        assert earliest_release(payload)["id"] == "r2"

    def test_missing_date_sorts_last(self) -> None:
        payload = {
            "release-list": [
                _release("r1", None),
                _release("r2", "2020-01-01"),
            ]
        }
        assert earliest_release(payload)["id"] == "r2"

    def test_empty_returns_none(self) -> None:
        assert earliest_release({"release-list": []}) is None


class TestPickRelease:
    def test_prefer_acquired_uses_acquired_mbid(self) -> None:
        payload = {
            "release-list": [
                _release("earliest", "1999"),
                _release("acquired", "2010"),
            ]
        }
        picked = pick_release(payload, prefer="acquired", acquired_mbid="acquired")
        assert picked["id"] == "acquired"

    def test_prefer_acquired_falls_back_to_earliest(self) -> None:
        payload = {
            "release-list": [
                _release("a", "1999"),
                _release("b", "2010"),
            ]
        }
        picked = pick_release(payload, prefer="acquired", acquired_mbid="zzz")
        assert picked["id"] == "a"

    def test_prefer_earliest_ignores_acquired(self) -> None:
        payload = {
            "release-list": [
                _release("a", "1999"),
                _release("b", "2010"),
            ]
        }
        picked = pick_release(payload, prefer="earliest", acquired_mbid="b")
        assert picked["id"] == "a"


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
            "label-info-list": [{"label": {"name": "Warp"}}],
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
