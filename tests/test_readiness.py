import os
from types import SimpleNamespace

import pytest

from shaper.readiness import PendingTracker, read_probe

WALL_OFFSET = 1_700_000_000.0
P = "/src/a.mkv"


class FakeFs:
    """Monotonic clock, wall clock and stat() over an in-memory set of files."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.files: dict[str, SimpleNamespace] = {}
        self.unreadable: set[str] = set()

    def mono(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.t + WALL_OFFSET

    def write(self, path: str, size: int) -> None:
        now = int(self.wall() * 1e9)
        self.files[path] = SimpleNamespace(
            st_size=size, st_mtime_ns=now, st_ctime_ns=now, st_mode=0o100644
        )

    def chmod(self, path: str, mode: int) -> None:
        self.files[path].st_mode = mode
        self.files[path].st_ctime_ns = int(self.wall() * 1e9)

    def stat(self, path: str) -> SimpleNamespace:
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def tracker(self, settle: float = 10.0) -> PendingTracker:
        return PendingTracker(
            settle, clock=self.mono, wall_clock=self.wall, stat=self.stat,  # type: ignore[arg-type]
            probe=lambda path, size: path not in self.unreadable,
        )  # fmt: skip


def ready(tr: PendingTracker) -> list[str]:
    return [p for p, _ in tr.poll()]


def test_fresh_file_waits_for_settle_window() -> None:
    fs = FakeFs()
    tr = fs.tracker()
    fs.write(P, 100)
    tr.touch(P)
    assert ready(tr) == []
    fs.t += 9.9
    assert ready(tr) == []
    fs.t += 0.2
    assert ready(tr) == [P]
    assert P not in tr


def test_growing_file_is_never_ready_while_it_changes() -> None:
    fs = FakeFs()
    tr = fs.tracker()
    fs.write(P, 100)
    tr.touch(P)
    for i in range(10):
        fs.t += 5
        fs.write(P, 100 * (i + 2))
        assert ready(tr) == []
    fs.t += 10
    assert ready(tr) == [P]


def test_old_file_found_by_scan_is_ready_immediately() -> None:
    fs = FakeFs()
    fs.write(P, 100)
    fs.t += 3600
    tr = fs.tracker()
    tr.touch(P)
    assert ready(tr) == [P]


def test_unreadable_file_waits_until_readable_and_settled() -> None:
    fs = FakeFs()
    tr = fs.tracker()
    fs.write(P, 100)
    fs.unreadable.add(P)
    tr.touch(P)
    fs.t += 20
    assert ready(tr) == []  # stable but unreadable
    assert P in tr
    fs.t += 5
    fs.chmod(P, 0o100644)  # the writer makes it readable when done
    fs.unreadable.clear()
    assert ready(tr) == []  # the chmod itself restarts the window
    fs.t += 9
    assert ready(tr) == []
    fs.t += 1.1
    assert ready(tr) == [P]


def test_deleted_file_is_dropped() -> None:
    fs = FakeFs()
    tr = fs.tracker()
    fs.write(P, 100)
    tr.touch(P)
    del fs.files[P]
    assert ready(tr) == []
    assert P not in tr


def test_defer() -> None:
    fs = FakeFs()
    fs.write(P, 100)
    fs.t += 3600
    tr = fs.tracker()
    tr.defer(P, 60)
    assert ready(tr) == []
    fs.t += 61
    assert ready(tr) == [P]


def test_discard_under() -> None:
    tr = FakeFs().tracker()
    for p in ("/src/a/1.mkv", "/src/a/b/2.mkv", "/src/ab/3.mkv"):
        tr.touch(p)
    tr.discard_under("/src/a")
    assert [p for p, _ in tr.snapshot()] == ["/src/ab/3.mkv"]


def test_read_probe_readable(tmp_path) -> None:
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 200_000)
    assert read_probe(str(f), 200_000)
    assert not read_probe(str(tmp_path / "missing.mkv"), 0)


@pytest.mark.linux
def test_read_probe_unreadable(tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 1000)
    f.chmod(0)
    assert not read_probe(str(f), 1000)
