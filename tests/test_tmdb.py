from datetime import date
from typing import Any

import httpx
import pytest

from shaper.state import State
from shaper.tmdb import API_BASE, Match, TmdbClient, TmdbError, normalize, pick_best

TOKEN = "eyJ" + "x" * 60


class FakeApi:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes  # path suffix -> json body, httpx.Response, or list of those
        self.requests: list[httpx.Request] = []
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/3/")
        route = self.routes.get(path)
        if isinstance(route, list):
            route = route.pop(0)
        if route is None:
            return httpx.Response(404)
        if callable(route):
            route = route(request)
        return route if isinstance(route, httpx.Response) else httpx.Response(200, json=route)

    def client(self, key: str = TOKEN) -> TmdbClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler), base_url=API_BASE)
        return TmdbClient(
            key, State(":memory:"), negative_ttl=3600, http=http,
            clock=lambda: self.now, sleep=self.slept.append,
        )  # fmt: skip


def results(*items: dict[str, Any]) -> dict[str, Any]:
    return {"results": list(items)}


MATRIX = {"id": 603, "title": "The Matrix", "original_title": "The Matrix",
          "release_date": "1999-03-30", "popularity": 80.0}  # fmt: skip


def test_normalize() -> None:
    assert normalize("Amélie & Friends: The_Movie!") == "amelie and friends the movie"


def test_movie_match_uses_year_and_canonical_title() -> None:
    api = FakeApi({"search/movie": results(MATRIX)})
    assert api.client().match_movie("the matrix", 1999) == Match(603, "The Matrix", 1999)
    assert api.requests[0].url.params["year"] == "1999"
    assert api.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_v3_key_is_sent_as_query_param() -> None:
    api = FakeApi({"search/movie": results(MATRIX)})
    api.client("0123456789abcdef0123456789abcdef").match_movie("The Matrix", None)
    assert api.requests[0].url.params["api_key"] == "0123456789abcdef0123456789abcdef"
    assert "Authorization" not in api.requests[0].headers


def test_movie_year_must_be_close_and_negative_result_is_cached() -> None:
    other = {"id": 1, "title": "Birthday", "release_date": "2015-01-01", "popularity": 5}
    api = FakeApi({"search/movie": lambda r: httpx.Response(200, json=results(other))})
    client = api.client()
    assert client.match_movie("Birthday", 2019) is None
    assert len(api.requests) == 2  # with year filter, then without
    assert client.match_movie("Birthday", 2019) is None
    assert len(api.requests) == 2  # cached
    api.now += 3601
    assert client.match_movie("Birthday", 2019) is None
    assert len(api.requests) == 4  # negative cache expired


def test_dissimilar_titles_do_not_match() -> None:
    api = FakeApi(
        {
            "search/movie": results(
                {**MATRIX, "title": "The Matrix Reloaded", "original_title": "The Matrix Reloaded"}
            )
        }
    )
    assert api.client().match_movie("IMG", None) is None


def test_series_prefers_matching_country() -> None:
    uk = {
        "id": 2996,
        "name": "The Office",
        "first_air_date": "2001-07-09",
        "origin_country": ["GB"],
        "popularity": 50,
    }
    us = {
        "id": 2316,
        "name": "The Office",
        "first_air_date": "2005-03-24",
        "origin_country": ["US"],
        "popularity": 40,
    }
    api = FakeApi({"search/tv": lambda r: httpx.Response(200, json=results(uk, us))})
    client = api.client()
    assert client.match_series("The Office", None, "US") == Match(2316, "The Office", 2005)
    assert client.match_series("The Office", None, None) == Match(2996, "The Office", 2001)


def test_series_retries_without_year() -> None:
    show = {"id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17"}
    api = FakeApi({"search/tv": [results(), results(show)]})
    assert api.client().match_series("Game of Thrones", 2019, None) == Match(
        1399, "Game of Thrones", 2011
    )
    assert api.requests[0].url.params["first_air_date_year"] == "2019"
    assert "first_air_date_year" not in api.requests[1].url.params


def test_episode_by_air_date() -> None:
    api = FakeApi({
        "tv/2224": {"seasons": [
            {"season_number": 0, "air_date": "2000-01-01"},
            {"season_number": 28, "air_date": "2023-01-09"},
            {"season_number": 29, "air_date": "2024-01-08"},
            {"season_number": 30, "air_date": "2025-01-06"},
        ]},
        "tv/2224/season/29": {"episodes": [
            {"episode_number": 30, "air_date": "2024-03-13"},
            {"episode_number": 31, "air_date": "2024-03-14"},
        ]},
    })  # fmt: skip
    assert api.client().episode_by_air_date(2224, date(2024, 3, 14)) == (29, 31)


def test_rate_limit_is_retried_after_delay() -> None:
    api = FakeApi({"search/movie": [
        httpx.Response(429, headers={"Retry-After": "3"}), results(MATRIX)
    ]})  # fmt: skip
    assert api.client().match_movie("The Matrix", 1999) is not None
    assert api.slept == [3.0]


@pytest.mark.parametrize("status", [401, 500])
def test_http_errors_raise(status: int) -> None:
    api = FakeApi({"search/movie": httpx.Response(status)})
    with pytest.raises(TmdbError):
        api.client().match_movie("The Matrix", 1999)


def test_network_errors_raise() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    api = FakeApi({"search/movie": boom})
    with pytest.raises(TmdbError):
        api.client().match_movie("The Matrix", 1999)


# -- fallback to alternative titles -----------------------------------------------------------

DARK_PHOENIX = {"id": 320288, "title": "Dark Phoenix", "original_title": "Dark Phoenix",
                "release_date": "2019-05-31", "popularity": 40.0}  # fmt: skip
PHOENIX_1998 = {"id": 4613, "title": "Phoenix", "original_title": "Phoenix",
                "release_date": "1998-09-12", "popularity": 5.0}  # fmt: skip


def alt_titles(*titles: str) -> dict[str, Any]:
    return {"titles": [{"iso_3166_1": "US", "title": t, "type": ""} for t in titles]}


def dark_phoenix_api() -> FakeApi:
    return FakeApi({
        "search/movie": results(DARK_PHOENIX, PHOENIX_1998),
        "movie/320288/alternative_titles": alt_titles("X-Men: Dark Phoenix", "Dark Phoenix 3D"),
        "movie/4613/alternative_titles": alt_titles("X-Men Dark Phoenix"),  # must never be asked
    })  # fmt: skip


def lookups(api: FakeApi) -> list[str]:
    return [r.url.path.removeprefix("/3/") for r in api.requests if "alternative" in r.url.path]


def test_title_known_only_as_an_alternative_title() -> None:
    api = dark_phoenix_api()
    assert api.client().match_movie("X-Men Dark Phoenix", 2019) == Match(
        320288, "Dark Phoenix", 2019
    )
    assert lookups(api) == ["movie/320288/alternative_titles"]  # once, exact-year results only


@pytest.mark.parametrize("year", [None, 2020])
def test_alternative_titles_need_the_exact_year(year: int | None) -> None:
    api = dark_phoenix_api()
    assert api.client().match_movie("X-Men Dark Phoenix", year) is None
    assert lookups(api) == []


def test_alternative_titles_are_not_fetched_for_a_direct_match() -> None:
    api = dark_phoenix_api()
    assert api.client().match_movie("Dark Phoenix", 2019) == Match(320288, "Dark Phoenix", 2019)
    assert lookups(api) == []


def test_dissimilar_alternative_titles_do_not_match() -> None:
    api = dark_phoenix_api()
    assert api.client().match_movie("X-Men Apocalypse", 2019) is None


# -- fallback to the file's own language ------------------------------------------------------

SONGBIRDS_EN = {"id": 695721, "title": "The Hunger Games: The Ballad of Songbirds & Snakes",
                "original_title": "The Hunger Games: The Ballad of Songbirds & Snakes",
                "release_date": "2023-11-15", "popularity": 90.0}  # fmt: skip
PANEM_DE = [
    {**SONGBIRDS_EN, "title": "Die Tribute von Panem – The Ballad of Songbirds and Snakes"},
    {"id": 70160, "title": "Die Tribute von Panem – The Hunger Games",
     "original_title": "The Hunger Games", "release_date": "2012-03-12", "popularity": 80.0},
    {"id": 131631, "title": "Die Tribute von Panem – Mockingjay Teil 1",
     "original_title": "The Hunger Games: Mockingjay - Part 1", "release_date": "2014-11-18",
     "popularity": 70.0},
]  # fmt: skip


def by_language(**bodies: dict[str, Any]) -> Any:
    return lambda r: httpx.Response(200, json=bodies[r.url.params["language"]])


def panem_api() -> FakeApi:
    return FakeApi({
        "search/movie": by_language(**{"en-US": results(SONGBIRDS_EN), "de": results(*PANEM_DE)}),
        "movie/695721": SONGBIRDS_EN,
    })  # fmt: skip


def test_localized_title_is_searched_in_the_files_language() -> None:
    api = panem_api()
    match = api.client().match_movie("Die Tribute von Panem", 2023, "de")
    assert match == Match(695721, "The Hunger Games: The Ballad of Songbirds & Snakes", 2023)
    languages = [(r.url.path.removeprefix("/3/"), r.url.params["language"]) for r in api.requests]
    assert languages == [
        ("search/movie", "en-US"), ("search/movie", "en-US"),  # with year filter, then without
        ("movie/695721/alternative_titles", "en-US"),  # the exact-year result's other names
        ("search/movie", "de"),
        ("movie/695721", "en-US"),  # folder is named in the configured language
    ]  # fmt: skip


def test_localized_main_title_needs_the_exact_year() -> None:
    # 2015 is within ±1 of Mockingjay Part 1 (2014), but a main-title-only match must be exact.
    assert panem_api().client().match_movie("Die Tribute von Panem", 2015, "de") is None


def test_no_fallback_without_a_foreign_language_tag() -> None:
    for language in (None, "en"):
        api = panem_api()
        assert api.client().match_movie("Die Tribute von Panem", 2023, language) is None
        assert {r.url.params["language"] for r in api.requests} == {"en-US"}


def test_localized_series_title() -> None:
    en = {"id": 71446, "name": "Money Heist", "original_name": "La casa de papel",
          "first_air_date": "2017-05-02"}  # fmt: skip
    api = FakeApi({
        "search/tv": by_language(
            **{"en-US": results(en), "de": results({**en, "name": "Haus des Geldes"})}
        ),
        "tv/71446": en,
    })  # fmt: skip
    assert api.client().match_series("Haus des Geldes", None, None, "de") == Match(
        71446, "Money Heist", 2017
    )


@pytest.mark.parametrize(
    ("query", "year", "allowed", "expected"),
    [
        ("Die Tribute von Panem", 2023, True, 695721),
        ("Die Tribute von Panem", 2023, False, None),  # only in the language fallback
        ("Die Tribute von Panem", None, True, None),  # needs a year
        ("Panem", 2023, True, None),  # needs more than one word
        ("Die Tribute", 2023, True, None),  # must be the whole main title
    ],
)
def test_main_title_matching(query: str, year: int | None, allowed: bool, expected: int) -> None:
    best = pick_best(
        [{**SONGBIRDS_EN, "title": "Die Tribute von Panem – The Ballad of Songbirds and Snakes"},
         {**SONGBIRDS_EN, "id": 1, "title": "Panem: Something", "original_title": ""}],
        query, title_keys=("title", "original_title"), date_key="release_date",
        year=year, year_tolerance=1, main_title_ok=allowed,
    )  # fmt: skip
    assert (best or {}).get("id") == expected
