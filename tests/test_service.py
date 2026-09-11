"""Service logic against real directories, without the watcher (ticks driven by hand)."""

import os
import shutil
from pathlib import Path

import pytest

from shaper.config import Config
from shaper.service import SENTINEL, Service
from shaper.state import IGNORED, State

pytestmark = pytest.mark.linux

MOVIE_LINK = "Movies/Some Movie (2019)/Some Movie (2019).mkv"


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    d = {name: tmp_path / name for name in ("src", "lib", "state")}
    for p in d.values():
        p.mkdir()
    return d


def service(dirs: dict[str, Path]) -> Service:
    cfg = Config(
        source_dirs=(dirs["src"],), library_dir=dirs["lib"], state_dir=dirs["state"],
        settle_seconds=0, min_size_bytes=0, require_tmdb_match=False,
    )  # fmt: skip
    svc = Service(cfg, State(":memory:"), None)
    svc.startup()
    svc.tick()
    return svc


def put(path: Path, data: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_links_movies_and_episodes_found_at_startup(dirs: dict[str, Path]) -> None:
    movie = put(dirs["src"] / "junk" / "some.movie.2019.1080p.mkv")
    ep = put(dirs["src"] / "Some.Show.S01E02.mkv")
    put(dirs["src"] / "IMG_1234.mp4")
    svc = service(dirs)
    assert os.readlink(dirs["lib"] / MOVIE_LINK) == str(movie)
    assert os.readlink(dirs["lib"] / "Shows/Some Show/Season 01/Some Show S01E02.mkv") == str(ep)
    memo = svc.state.memo_get(str(dirs["src"] / "IMG_1234.mp4"))
    assert memo is not None and memo.status == IGNORED


def test_dangling_links_removed_on_reconcile(dirs: dict[str, Path]) -> None:
    movie = put(dirs["src"] / "Some.Movie.2019.mkv")
    put(dirs["src"] / "keep" / "other.txt")
    svc = service(dirs)
    movie.unlink()
    svc.reconcile()
    assert not os.path.lexists(dirs["lib"] / MOVIE_LINK)
    assert not (dirs["lib"] / "Movies" / "Some Movie (2019)").exists()


def test_empty_source_keeps_links(dirs: dict[str, Path]) -> None:
    put(dirs["src"] / "Some.Movie.2019.mkv")
    svc = service(dirs)
    for child in dirs["src"].iterdir():  # looks like an unmounted drive
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    svc.reconcile()
    assert os.path.lexists(dirs["lib"] / MOVIE_LINK)
    put(dirs["src"] / "unrelated.txt")  # mounted again, but the movie is really gone
    svc.reconcile()
    assert not os.path.lexists(dirs["lib"] / MOVIE_LINK)


def test_missing_sentinel_keeps_links(dirs: dict[str, Path]) -> None:
    put(dirs["src"] / SENTINEL, b"")
    movie = put(dirs["src"] / "Some.Movie.2019.mkv")
    put(dirs["src"] / "other.txt")
    svc = service(dirs)
    (dirs["src"] / SENTINEL).unlink()
    movie.unlink()
    svc.reconcile()
    assert os.path.lexists(dirs["lib"] / MOVIE_LINK)


def test_links_outside_sources_are_removed(dirs: dict[str, Path]) -> None:
    stray = dirs["lib"] / "Movies" / "Old (2000)" / "Old (2000).mkv"
    stray.parent.mkdir(parents=True)
    os.symlink("/media/removed-source/old.mkv", stray)
    service(dirs)
    assert not os.path.lexists(stray)


def test_duplicate_movie_becomes_second_version(dirs: dict[str, Path]) -> None:
    put(dirs["src"] / "a" / "Some.Movie.2019.1080p.mkv")
    put(dirs["src"] / "b" / "Some.Movie.2019.2160p.mkv")
    service(dirs)
    folder = dirs["lib"] / "Movies" / "Some Movie (2019)"
    names = set(os.listdir(folder))
    base = "Some Movie (2019).mkv"
    assert base in names and len(names) == 2
    # Whichever file came second is labelled with its own resolution.
    (other,) = names - {base}
    label = other.removeprefix("Some Movie (2019) - ").removesuffix(".mkv")
    assert label in {"1080p", "2160p"}
    assert label in os.readlink(folder / other)
