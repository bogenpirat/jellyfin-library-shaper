from datetime import date

import pytest

from shaper.classify import Ignored, Kind, ParsedMedia, classify


@pytest.mark.parametrize(
    ("path", "title", "year", "part"),
    [
        ("Movie.Name.2019.1080p.BluRay.x264-GRP.mkv", "Movie Name", 2019, None),
        ("Movie Name (2010) CD1.avi", "Movie Name", 2010, 1),
        ("Movie.Name.2010.Part.2.mkv", "Movie Name", 2010, 2),
        ("Blade Runner 2049 (2017).mkv", "Blade Runner 2049", 2017, None),
        ("2001.A.Space.Odyssey.1968.mkv", "2001 A Space Odyssey", 1968, None),
        ("The.Interview.2014.1080p.mkv", "The Interview", 2014, None),
        ("Downloads/Movie.Name.2019.1080p/movie.name.2019.1080p.mkv", "movie name", 2019, None),
    ],
)
def test_movies(path: str, title: str, year: int, part: int | None) -> None:
    p = classify(path)
    assert isinstance(p, ParsedMedia)
    assert (p.kind, p.title, p.year, p.part) == (Kind.MOVIE, title, year, part)


def test_movie_details() -> None:
    p = classify("Alien.1979.Directors.Cut.1080p.BluRay-GRP.mkv")
    assert isinstance(p, ParsedMedia)
    assert (p.screen_size, p.edition, p.release_group, p.extension) == (
        "1080p", "Director's Cut", "GRP", ".mkv",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("path", "title", "season", "episodes"),
    [
        ("The.Office.US.S02E05.720p.WEB.mkv", "The Office", 2, (5,)),
        ("Show Name/Season 2/05.mkv", "Show Name", 2, (5,)),
        ("Show.Name.S02/05.mkv", "Show Name", 2, (5,)),
        ("Some.Show.S01E01E02.1080p.mkv", "Some Show", 1, (1, 2)),
        ("Some.Show.S01E01-E03.1080p.mkv", "Some Show", 1, (1, 2, 3)),
        ("Doctor.Who.2005.S00E01.Special.mkv", "Doctor Who", 0, (1,)),
        ("[SubsPlease] Frieren - 12 (1080p) [ABCD1234].mkv", "Frieren", 1, (12,)),
        ("Some Show (2010)/Season 01/Some Show - 1x03 - Title.mkv", "Some Show", 1, (3,)),
        ("Show.Name.S01-S03.COMPLETE/Show.Name.S02E03.mkv", "Show Name", 2, (3,)),
        ("tv/Firefly/Firefly - S01E01 - Serenity.avi", "Firefly", 1, (1,)),
    ],
)
def test_episodes(path: str, title: str, season: int, episodes: tuple[int, ...]) -> None:
    p = classify(path)
    assert isinstance(p, ParsedMedia)
    assert (p.kind, p.title, p.season, p.episodes) == (Kind.EPISODE, title, season, episodes)


def test_episode_country() -> None:
    p = classify("The.Office.US.S02E05.720p.WEB.mkv")
    assert isinstance(p, ParsedMedia) and p.country == "US"


def test_date_based_episode() -> None:
    p = classify("The.Daily.Show.2024.03.14.Some.Guest.720p.mkv")
    assert isinstance(p, ParsedMedia)
    assert (p.kind, p.title, p.air_date, p.episodes) == (
        Kind.EPISODE, "The Daily Show", date(2024, 3, 14), (),
    )  # fmt: skip


@pytest.mark.parametrize(
    "path",
    [
        "Movie.Name.2019.1080p-sample.mkv",
        "Movie.Name.2019.1080p/Sample/movie-sample.mkv",
        "Movie.Name.2019.1080p/sample.mkv",
        "Movie.Name.2019.Trailer.1080p.mkv",
        "Movie.Name.2019-featurette.mkv",
        "Movie.Name.2019.1080p/Extras/Behind the scenes.mkv",
        "Featurettes/Making of.mkv",
        "IMG_1234.mp4",
        "VID_20230101_123456.mp4",
        "PXL_20240101_123456789.mp4",
        "Some.Show.S01.1080p.mkv",
    ],
)
def test_ignored(path: str) -> None:
    assert isinstance(classify(path), Ignored)
