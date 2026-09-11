from datetime import date
from typing import Any

import httpx
import pytest

from shaper.state import State
from shaper.tmdb import API_BASE, Match, TmdbClient, TmdbError, normalize

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
