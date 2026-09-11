import itertools

import pytest

from shaper import naming
from shaper.classify import Kind, ParsedMedia


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Mission: Impossible", "Mission - Impossible"),
        ('What? "Now" <x>|y*', "What Now x y"),
        ("Title...", "Title"),
        ("AC/DC Live", "AC DC Live"),
        ("", "Unknown"),
    ],
)
def test_sanitize(raw: str, clean: str) -> None:
    assert naming.sanitize(raw) == clean


def test_media_folder() -> None:
    assert naming.media_folder("Alien", 1979, 348) == "Alien (1979) [tmdbid-348]"
    assert naming.media_folder("Alien", None, None) == "Alien"
    assert naming.tmdb_id_of_folder("Alien (1979) [tmdbid-348]") == 348
    assert naming.tmdb_id_of_folder("Alien (1979)") is None


def test_episode_code() -> None:
    assert naming.episode_code(1, (1,)) == "S01E01"
    assert naming.episode_code(0, (3,)) == "S00E03"
    assert naming.episode_code(2, (1, 2, 3)) == "S02E01-E03"
    assert naming.season_folder(0) == "Season 00"
    assert naming.season_folder(12) == "Season 12"


def _movie(**kw: object) -> ParsedMedia:
    return ParsedMedia(Kind.MOVIE, "X", ".mkv", **kw)  # type: ignore[arg-type]


def test_version_labels() -> None:
    labels = list(
        itertools.islice(naming.version_labels(_movie(screen_size="1080p", release_group="GRP")), 5)
    )
    assert labels == [None, "1080p", "1080p GRP", "(2)", "(3)"]
    assert list(itertools.islice(naming.version_labels(_movie()), 3)) == [None, "(2)", "(3)"]


def test_file_names() -> None:
    folder = "The Matrix (1999) [tmdbid-603]"
    assert naming.movie_file(folder, _movie(), None) == f"{folder}.mkv"
    assert naming.movie_file(folder, _movie(), "1080p") == f"{folder} - 1080p.mkv"
    assert naming.movie_file(folder, _movie(part=2), None) == f"{folder}-cd2.mkv"
    ep = ParsedMedia(Kind.EPISODE, "x", ".mp4")
    assert naming.episode_file("Show: X", 1, (2, 3), ep, None) == "Show - X S01E02-E03.mp4"
    assert naming.episode_file("Show", 1, (2,), ep, "720p") == "Show S01E02 - 720p.mp4"
