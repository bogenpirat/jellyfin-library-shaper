"""Track files that may still be written to, and report them once they are finished.

A file is ready when its fingerprint (size, mtime, ctime, mode) has not changed for the settle
window and it can actually be read. The window is measured from the file's own ctime, which the
kernel bumps on every write, chmod, utime and rename, so a file found by a rescan long after it was
written becomes ready immediately, while a freshly touched one waits out the full window.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

PROBE_BYTES = 64 * 1024

type Fingerprint = tuple[int, int, int, int]


def fingerprint(st: os.stat_result) -> Fingerprint:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)


def read_probe(path: str, size: int) -> bool:
    """Read the head and tail of the file; fails while the writer keeps it unreadable."""
    try:
        with open(path, "rb") as f:
            f.read(PROBE_BYTES)
            if size > PROBE_BYTES:
                f.seek(size - PROBE_BYTES)
                f.read(PROBE_BYTES)
    except OSError:
        return False
    return True


@dataclass
class _Entry:
    fp: Fingerprint | None = None
    stable_since: float = 0.0
    not_before: float = 0.0


class PendingTracker:
    def __init__(
        self,
        settle_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        stat: Callable[[str], os.stat_result] = os.stat,
        probe: Callable[[str, int], bool] = read_probe,
    ) -> None:
        self.settle_seconds = settle_seconds
        self._clock = clock
        self._wall = wall_clock
        self._stat = stat
        self._probe = probe
        self._entries: dict[str, _Entry] = {}

    def __contains__(self, path: str) -> bool:
        return path in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def touch(self, path: str) -> None:
        """Something happened to path: (re)start watching it; the next poll re-stats it."""
        self._entries[path] = _Entry()

    def defer(self, path: str, delay: float) -> None:
        """Re-check path, but not before `delay` seconds from now (e.g. after a TMDB outage)."""
        self._entries[path] = _Entry(not_before=self._clock() + delay)

    def discard(self, path: str) -> None:
        self._entries.pop(path, None)

    def discard_under(self, directory: str) -> None:
        prefix = directory.rstrip("/") + "/"
        for p in [p for p in self._entries if p.startswith(prefix)]:
            del self._entries[p]

    def snapshot(self) -> list[tuple[str, float | None]]:
        """(path, seconds until the settle window elapses, None if not yet stat'ed)."""
        now = self._clock()
        return [
            (p, None if e.fp is None else max(0.0, e.stable_since + self.settle_seconds - now))
            for p, e in sorted(self._entries.items())
        ]

    def poll(self) -> list[tuple[str, os.stat_result]]:
        """Re-stat every pending file; remove and return those that are ready."""
        now = self._clock()
        ready: list[tuple[str, os.stat_result]] = []
        for path, entry in list(self._entries.items()):
            if now < entry.not_before:
                continue
            try:
                st = self._stat(path)
            except FileNotFoundError:
                del self._entries[path]
                continue
            except OSError:
                entry.fp = None  # e.g. a parent dir not yet traversable; keep waiting
                continue
            fp = fingerprint(st)
            if fp != entry.fp:
                entry.fp = fp
                changed_at = max(st.st_mtime_ns, st.st_ctime_ns) / 1e9
                entry.stable_since = now - max(0.0, self._wall() - changed_at)
            if now - entry.stable_since < self.settle_seconds:
                continue
            if self._probe(path, st.st_size):
                del self._entries[path]
                ready.append((path, st))
            else:
                entry.stable_since = now  # unreadable: give the writer another full window
        return ready
