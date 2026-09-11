"""Whole service with real inotify, a mocked TMDB, and a slow writer."""

import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from shaper.config import Config
from shaper.service import Service
from shaper.state import State
from shaper.tmdb import API_BASE, TmdbClient
from shaper.watcher import FsEvent, start_observer

pytestmark = pytest.mark.linux

SETTLE = 1.0
SHOW_DIR = "Shows/Some Show (2011) [tmdbid-1399]"
SHOW_LINK = f"{SHOW_DIR}/Season 01/Some Show S01E02.mkv"
MATRIX_DIR = "Movies/The Matrix (1999) [tmdbid-603]"


def tmdb_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/search/movie"):
        return httpx.Response(200, json={"results": [
            {"id": 603, "title": "The Matrix", "release_date": "1999-03-30", "popularity": 80}
        ]})  # fmt: skip
    if request.url.path.endswith("/search/tv"):
        return httpx.Response(200, json={"results": [
            {"id": 1399, "name": "Some Show", "first_air_date": "2011-04-17"}
        ]})  # fmt: skip
    return httpx.Response(404)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, Path]]:
    d = {name: tmp_path / name for name in ("src1", "src2", "lib", "state")}
    for p in d.values():
        p.mkdir()
    (d["src1"] / "placeholder.txt").write_text("keeps the source non-empty")
    (d["src2"] / "placeholder.txt").write_text("keeps the source non-empty")
    cfg = Config(
        source_dirs=(d["src1"], d["src2"]), library_dir=d["lib"], state_dir=d["state"],
        settle_seconds=SETTLE, poll_interval=0.1, min_size_bytes=0, tmdb_api_key="t" * 40,
    )  # fmt: skip
    state = State(":memory:")
    http = httpx.Client(transport=httpx.MockTransport(tmdb_handler), base_url=API_BASE)
    svc = Service(cfg, state, TmdbClient("t" * 40, state, http=http))
    events: queue.Queue[FsEvent] = queue.Queue()
    stop = threading.Event()
    observer = start_observer(svc.sources, "inotify", events)
    svc.startup()
    thread = threading.Thread(target=svc.run, args=(events, stop), daemon=True)
    thread.start()
    yield d
    stop.set()
    thread.join(5)
    observer.stop()
    observer.join(5)


def wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_slow_write_is_linked_only_after_it_settles(env: dict[str, Path]) -> None:
    src = env["src1"] / "incoming" / "Some.Show.S01E02.1080p.mkv"
    src.parent.mkdir()
    link = env["lib"] / SHOW_LINK
    with open(src, "wb") as f:
        for _ in range(6):
            f.write(os.urandom(64 * 1024))
            f.flush()
            last_write = time.monotonic()
            time.sleep(SETTLE * 0.4)
            assert not os.path.lexists(link), "linked while still being written"
    assert wait_for(lambda: os.path.lexists(link))
    assert time.monotonic() - last_write >= SETTLE * 0.9
    assert os.readlink(link) == str(src)

    src.unlink()
    assert wait_for(lambda: not os.path.lexists(link))
    assert not (env["lib"] / SHOW_DIR).exists()
    assert (env["lib"] / "Shows").is_dir()


def test_unreadable_file_is_linked_once_readable(env: dict[str, Path]) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    src = env["src1"] / "The.Matrix.1999.mkv"
    src.write_bytes(os.urandom(1024))
    src.chmod(0)
    time.sleep(SETTLE * 2.5)
    assert not (env["lib"] / MATRIX_DIR).exists()
    src.chmod(0o644)
    assert wait_for(lambda: (env["lib"] / MATRIX_DIR / "The Matrix (1999) [tmdbid-603].mkv")
                    .is_symlink())  # fmt: skip


def test_same_movie_in_two_sources_becomes_two_versions(env: dict[str, Path]) -> None:
    a = env["src1"] / "The.Matrix.1999.1080p.mkv"
    b = env["src2"] / "Matrix" / "The Matrix (1999).mkv"
    b.parent.mkdir()
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    folder = env["lib"] / MATRIX_DIR
    assert wait_for(lambda: folder.is_dir() and len(os.listdir(folder)) == 2)
    assert {os.readlink(folder / n) for n in os.listdir(folder)} == {str(a), str(b)}


def test_moving_a_directory_away_removes_its_links(env: dict[str, Path]) -> None:
    d = env["src1"] / "batch"
    d.mkdir()
    (d / "Some.Show.S01E02.mkv").write_bytes(b"x")
    link = env["lib"] / SHOW_LINK
    assert wait_for(lambda: os.path.lexists(link))
    os.rename(d, env["src1"].parent / "elsewhere")
    assert wait_for(lambda: not os.path.lexists(link))
