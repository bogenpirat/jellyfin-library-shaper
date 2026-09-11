"""Turn a source-relative path into a movie or episode description, or a reason to ignore it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from guessit import guessit

from .scanner import in_extras_dir

# Phone/camera/screen-recorder names: guessit turns e.g. IMG_1234 into "IMG" S12E34.
_CAMERA = re.compile(
    r"^(img|vid|dsc[fn]?|mov|mvi|pxl|gopr|gh\d\d|dji|wp|screen[ _-]?recording)[ _-]?\d", re.I
)
# Jellyfin's extras filename suffixes (https://jellyfin.org/docs/general/server/media/movies/).
_EXTRA_SUFFIX = re.compile(
    r"([-._](trailer|sample)|-(scene|clip|interview|behindthescenes|deleted|deletedscene"
    r"|featurette|short|other|extra))$",
    re.I,
)


class Kind(StrEnum):
    MOVIE = "movie"
    EPISODE = "episode"


@dataclass(frozen=True, slots=True)
class ParsedMedia:
    kind: Kind
    title: str
    extension: str
    year: int | None = None
    season: int | None = None
    episodes: tuple[int, ...] = ()
    air_date: date | None = None
    part: int | None = None
    screen_size: str | None = None
    release_group: str | None = None
    edition: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class Ignored:
    reason: str


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _first(value: Any) -> Any:
    items = _as_list(value)
    return items[0] if items else None


def _str(value: Any) -> str | None:
    v = _first(value)
    return str(v).strip() or None if v is not None else None


def classify(rel_path: str | PurePosixPath) -> ParsedMedia | Ignored:
    rel = PurePosixPath(rel_path)
    stem = rel.stem
    if in_extras_dir(rel):
        return Ignored("inside an extras folder")
    if stem.lower() in {"trailer", "sample"} or _EXTRA_SUFFIX.search(stem):
        return Ignored("extra (by filename)")
    if _CAMERA.match(stem):
        return Ignored("camera/phone recording")

    g = guessit(str(rel))
    other = {str(o).lower() for o in _as_list(g.get("other"))}
    for tag in ("sample", "trailer", "extras"):
        if tag in other:
            return Ignored(tag)

    title = _str(g.get("title"))
    if not title:
        return Ignored("no title found")
    year = _first(g.get("year"))
    country = getattr(_first(g.get("country")), "alpha2", None)
    common: dict[str, Any] = {
        "title": title,
        "extension": rel.suffix.lower(),
        "year": int(year) if year else None,
        "screen_size": _str(g.get("screen_size")),
        "release_group": _str(g.get("release_group")),
        "edition": _str(g.get("edition")),
        "country": country,
    }

    if g.get("type") == "episode":
        seasons = [int(s) for s in _as_list(g.get("season"))]
        episodes = tuple(sorted({int(e) for e in _as_list(g.get("episode"))}))
        if episodes:
            # No season (e.g. absolute-numbered anime): best effort, season 1.
            season = seasons[0] if seasons else 1
            return ParsedMedia(Kind.EPISODE, season=season, episodes=episodes, **common)
        air_date = g.get("date")
        if isinstance(air_date, date):
            return ParsedMedia(Kind.EPISODE, air_date=air_date, **common)
        return Ignored("episode without episode number or air date")

    part = _first(g.get("cd")) or _first(g.get("part"))
    return ParsedMedia(Kind.MOVIE, part=int(part) if part else None, **common)
