"""TMDB lookups: canonical title/year/id for movies and series, and air date -> episode number.

A file tagged with another language than TMDB_LANGUAGE (e.g. "German") that finds no match is
searched again in its own language, because its title is then usually the localized one.

A movie whose main title doesn't match (with an exact year) may still match one of its
alternative titles ("X-Men: Dark Phoenix" for "Dark Phoenix").

Results (including "no match") are cached in sqlite. Positive results never expire, which keeps
every episode of a series in the same folder; negative results expire after the configured TTL.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import httpx

from .state import State

API_BASE = "https://api.themoviedb.org/3/"
MIN_SIMILARITY = 0.85
MAX_RETRY_AFTER = 30.0
MIN_PREFIX_WORDS = 2
MAX_ALT_TITLE_LOOKUPS = 3
_V3_KEY = re.compile(r"^[0-9a-f]{32}$", re.I)
# Where a subtitle starts: "Title: Subtitle", "Title – Subtitle", "Title - Subtitle".
_SUBTITLE = re.compile(r"\s*:\s+|\s+[-–—]\s+")

log = logging.getLogger(__name__)


class TmdbError(Exception):
    """Transient failure (network, 5xx, rate limit, bad key); the caller should retry later."""


@dataclass(frozen=True, slots=True)
class Match:
    tmdb_id: int | None
    title: str
    year: int | None


def normalize(title: str) -> str:
    s = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def _year(value: str | None) -> int | None:
    return int(value[:4]) if value and value[:4].isdigit() else None


def _similarity(normalized_query: str, titles: Iterable[str]) -> float:
    return max(
        (difflib.SequenceMatcher(None, normalized_query, normalize(t)).ratio() for t in titles),
        default=0.0,
    )


def _main_title(title: str) -> str | None:
    """The part before the subtitle ("Die Tribute von Panem" of "Die Tribute von Panem – …")."""
    head = _SUBTITLE.split(title, maxsplit=1)
    return head[0] if len(head) > 1 else None


def pick_best(
    results: Iterable[dict[str, Any]],
    query: str,
    *,
    title_keys: tuple[str, ...],
    date_key: str,
    year: int | None = None,
    year_tolerance: int | None = None,
    country: str | None = None,
    main_title_ok: bool = False,
) -> dict[str, Any] | None:
    """Best result whose title is close enough to the query (and year, if constrained).

    With `main_title_ok`, a query equal to just the main title of a subtitled result also
    counts (as the weakest acceptable match), but only with an exact year and a multi-word query:
    localized releases often drop the subtitle ("Die Tribute von Panem" for "… – The Ballad of
    Songbirds and Snakes").

    Ranked by similarity, then exact year, then origin country, then popularity.
    """
    q = normalize(query)
    best, best_key = None, None
    for r in results:
        raw_titles = [r[k] for k in title_keys if r.get(k)]
        sim = _similarity(q, raw_titles)
        ry = _year(r.get(date_key))
        if (
            sim < MIN_SIMILARITY
            and main_title_ok
            and year is not None
            and ry == year
            and len(q.split()) >= MIN_PREFIX_WORDS
            and any(normalize(_main_title(t) or "") == q for t in raw_titles)
        ):
            sim = MIN_SIMILARITY
        if sim < MIN_SIMILARITY:
            continue
        if (
            year is not None
            and year_tolerance is not None
            and (ry is None or abs(ry - year) > year_tolerance)
        ):
            continue
        key = (
            round(sim, 2),
            year is not None and ry == year,
            country is not None and country in (r.get("origin_country") or []),
            r.get("popularity") or 0.0,
        )
        if best_key is None or key > best_key:
            best, best_key = r, key
    return best


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        state: State,
        *,
        language: str = "en-US",
        negative_ttl: float = 24 * 3600,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._state = state
        self._language = language
        self._negative_ttl = negative_ttl
        self._clock = clock
        self._sleep = sleep
        if _V3_KEY.match(api_key):
            self._auth_params, self._auth_headers = {"api_key": api_key}, {}
        else:  # v4 "API Read Access Token"
            self._auth_params, self._auth_headers = {}, {"Authorization": f"Bearer {api_key}"}
        self._http = http or httpx.Client(base_url=API_BASE, timeout=15.0)

    def close(self) -> None:
        self._http.close()

    # -- public lookups -------------------------------------------------------------------------

    def match_movie(
        self, title: str, year: int | None, language: str | None = None
    ) -> Match | None:
        alt = self._fallback_language(language)
        key = f"movie:{normalize(title)}:{year or ''}" + (f":{alt}" if alt else "")
        data = self._cached(
            key,
            lambda: (
                self._search_movie(title, year)
                or (self._search_movie(title, year, alt) if alt else None)
            ),
        )
        return Match(**data) if data else None

    def match_series(
        self, title: str, year: int | None, country: str | None, language: str | None = None
    ) -> Match | None:
        alt = self._fallback_language(language)
        key = f"tv:{normalize(title)}:{year or ''}:{country or ''}" + (f":{alt}" if alt else "")
        data = self._cached(
            key,
            lambda: (
                self._search_tv(title, year, country)
                or (self._search_tv(title, year, country, alt) if alt else None)
            ),
        )
        return Match(**data) if data else None

    def episode_by_air_date(self, series_id: int, air_date: date) -> tuple[int, int] | None:
        key = f"airdate:{series_id}:{air_date.isoformat()}"
        data = self._cached(key, lambda: self._find_by_air_date(series_id, air_date))
        return (data["season"], data["episode"]) if data else None

    # -- searches -------------------------------------------------------------------------------

    def _fallback_language(self, language: str | None) -> str | None:
        """The file's language, if it differs from the configured one (else a retry is moot)."""
        if not language:
            return None
        same = language.split("-")[0].lower() == self._language.split("-")[0].lower()
        return None if same else language

    def _search_movie(
        self, title: str, year: int | None, language: str | None = None
    ) -> dict[str, Any] | None:
        """Search in the configured language, or (as a fallback) in the file's `language`."""
        # With a year: first ask TMDB to filter by it, then search unfiltered but still require the
        # year within ±1 (festival vs. release year) so "Birthday 2019" can't match any "Birthday".
        seen: dict[int, dict[str, Any]] = {}
        for year_param in [year, None] if year else [None]:
            results = self._get(
                "search/movie", query=title, year=year_param, language=language
            ).get("results", [])
            best = pick_best(
                results, title, title_keys=("title", "original_title"), date_key="release_date",
                year=year, year_tolerance=1, main_title_ok=language is not None,
            )  # fmt: skip
            if best:
                return self._movie_match(best, title, language)
            seen.update((r["id"], r) for r in results if r["id"] not in seen)
        best = self._by_alternative_title(seen.values(), title, year)
        return self._movie_match(best, title, language) if best else None

    def _by_alternative_title(
        self, results: Iterable[dict[str, Any]], title: str, year: int | None
    ) -> dict[str, Any] | None:
        """First of the top exact-year results that is also known by (about) `title`.

        TMDB's main title can be shorter than the name a release uses: "Dark Phoenix" is listed
        with "X-Men: Dark Phoenix" only as an alternative title. Search finds it by that name, but
        the similarity check against the main title rejects it.
        """
        if year is None:
            return None
        q = normalize(title)
        same_year = [r for r in results if _year(r.get("release_date")) == year]
        for r in same_year[:MAX_ALT_TITLE_LOOKUPS]:
            alts = self._get(f"movie/{r['id']}/alternative_titles").get("titles", [])
            if _similarity(q, [a["title"] for a in alts if a.get("title")]) >= MIN_SIMILARITY:
                return r
        return None

    def _movie_match(
        self, best: dict[str, Any], title: str, language: str | None
    ) -> dict[str, Any]:
        if language:  # name the folder in the configured language, not the file's
            best = self._get(f"movie/{best['id']}") or best
        year = _year(best.get("release_date"))
        return asdict(Match(best["id"], best.get("title") or title, year))

    def _search_tv(
        self, title: str, year: int | None, country: str | None, language: str | None = None
    ) -> dict[str, Any] | None:
        """Search in the configured language, or (as a fallback) in the file's `language`."""
        # A year in an episode name is usually, but not reliably, the series' first-air year, so the
        # unfiltered retry drops the year constraint.
        attempts: list[tuple[int | None, int | None]] = [(year, 1)] if year else []
        attempts.append((None, None))
        for year_param, tolerance in attempts:
            results = self._get(
                "search/tv", query=title, first_air_date_year=year_param, language=language
            ).get("results", [])
            best = pick_best(
                results, title, title_keys=("name", "original_name"), date_key="first_air_date",
                year=year_param, year_tolerance=tolerance, country=country,
                main_title_ok=language is not None,
            )  # fmt: skip
            if best:
                if language:  # name the folder in the configured language, not the file's
                    best = self._get(f"tv/{best['id']}") or best
                return asdict(
                    Match(best["id"], best.get("name") or title, _year(best.get("first_air_date")))
                )
        return None

    def _find_by_air_date(self, series_id: int, air_date: date) -> dict[str, int] | None:
        target = air_date.isoformat()
        seasons = [
            s for s in self._get(f"tv/{series_id}").get("seasons", [])
            if s.get("air_date") and s["air_date"] <= target
        ]  # fmt: skip
        # The two latest-starting regular seasons that began on or before the date, then specials.
        regular = sorted(
            (s for s in seasons if s.get("season_number")),
            key=lambda s: s["air_date"],
            reverse=True,
        )[:2]
        specials = [s for s in seasons if s.get("season_number") == 0]
        for season in regular + specials:
            number = season["season_number"]
            for ep in self._get(f"tv/{series_id}/season/{number}").get("episodes", []):
                if ep.get("air_date") == target:
                    return {"season": number, "episode": ep["episode_number"]}
        return None

    # -- plumbing -------------------------------------------------------------------------------

    def _cached(self, key: str, fetch: Callable[[], Any]) -> Any:
        now = self._clock()
        hit = self._state.cache_get(key)
        if hit is not None:
            payload, fetched_at = hit
            if payload is not None or now - fetched_at < self._negative_ttl:
                return payload
        payload = fetch()
        self._state.cache_put(key, payload, now)
        return payload

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = {"language": self._language, "include_adult": "false"}
        query |= {k: v for k, v in params.items() if v is not None} | self._auth_params
        for _ in range(4):
            try:
                resp = self._http.get(path, params=query, headers=self._auth_headers)
            except httpx.HTTPError as e:
                raise TmdbError(f"{path}: {e}") from e
            if resp.status_code == 429:
                try:
                    wait = float(resp.headers.get("Retry-After", "2"))
                except ValueError:
                    wait = 2.0
                self._sleep(min(max(wait, 1.0), MAX_RETRY_AFTER))
                continue
            if resp.status_code == 404:
                return {}
            if resp.status_code in (401, 403):
                raise TmdbError(f"{path}: HTTP {resp.status_code} (check TMDB_API_KEY)")
            if resp.status_code >= 400:
                raise TmdbError(f"{path}: HTTP {resp.status_code}")
            return resp.json()
        raise TmdbError(f"{path}: still rate limited")
