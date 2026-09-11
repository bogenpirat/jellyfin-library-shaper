"""Jellyfin folder and file names.

Movies: `Title (Year) [tmdbid-N]/Title (Year) [tmdbid-N][ - label][-cdN].ext`
Shows:  `Title (Year) [tmdbid-N]/Season NN/Title SNNENN[-ENN][ - label].ext`
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator

from .classify import ParsedMedia

_COLON = re.compile(r"\s*:\s*")
# Characters Jellyfin reserves, plus control characters.
_RESERVED = re.compile(r'[<>"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")
_TMDB_TAG = re.compile(r"\[tmdbid-(\d+)\]")


def sanitize(name: str) -> str:
    s = _COLON.sub(" - ", name)
    s = _RESERVED.sub(" ", s)
    s = _SPACES.sub(" ", s).strip().rstrip(". ").strip()
    return s or "Unknown"


def media_folder(title: str, year: int | None, tmdb_id: int | None) -> str:
    parts = [sanitize(title)]
    if year:
        parts.append(f"({year})")
    if tmdb_id:
        parts.append(f"[tmdbid-{tmdb_id}]")
    return " ".join(parts)


def tmdb_id_of_folder(folder_name: str) -> int | None:
    m = _TMDB_TAG.search(folder_name)
    return int(m.group(1)) if m else None


def season_folder(season: int) -> str:
    return f"Season {season:02d}"


def episode_code(season: int, episodes: tuple[int, ...]) -> str:
    code = f"S{season:02d}E{episodes[0]:02d}"
    if len(episodes) > 1:
        code += f"-E{episodes[-1]:02d}"
    return code


def version_labels(p: ParsedMedia) -> Iterator[str | None]:
    """Candidate version labels, tried in order until a free file name is found."""
    seen: set[str] = set()
    raw = [
        p.screen_size,
        p.edition,
        " ".join(x for x in (p.screen_size, p.edition) if x),
        " ".join(x for x in (p.screen_size, p.release_group) if x),
    ]
    yield None
    for label in raw:
        if label and (clean := sanitize(label)) not in seen:
            seen.add(clean)
            yield clean
    for n in itertools.count(2):
        yield f"({n})"


def movie_file(folder: str, p: ParsedMedia, label: str | None) -> str:
    name = folder
    if label:
        name += f" - {label}"
    if p.part:
        name += f"-cd{p.part}"
    return name + p.extension


def episode_file(
    series_title: str, season: int, episodes: tuple[int, ...], p: ParsedMedia, label: str | None
) -> str:
    name = f"{sanitize(series_title)} {episode_code(season, episodes)}"
    if label:
        name += f" - {label}"
    return name + p.extension
