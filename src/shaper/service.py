"""Main loop: route filesystem events, turn finished files into links, reconcile periodically."""

from __future__ import annotations

import logging
import os
import queue
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from . import naming, scanner
from .classify import Ignored, Kind, ParsedMedia, classify
from .config import Config
from .linker import Linker
from .readiness import PendingTracker
from .state import IGNORED, UNMATCHED, Memo, State, write_status
from .tmdb import Match, TmdbClient, TmdbError
from .watcher import EventKind, FsEvent

SENTINEL = ".shaper-sentinel"
TMDB_RETRY_SECONDS = 300.0

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Placement:
    match: Match
    season: int | None = None
    episodes: tuple[int, ...] = ()


class Service:
    def __init__(
        self,
        cfg: Config,
        state: State,
        tmdb: TmdbClient | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.tmdb = tmdb
        self.sources = [os.path.normpath(s) for s in cfg.source_dirs]
        self.movies_dir = os.path.normpath(cfg.movies_dir)
        self.shows_dir = os.path.normpath(cfg.shows_dir)
        self.linker = Linker(
            os.path.normpath(cfg.library_dir), [self.movies_dir, self.shows_dir],
            dry_run=cfg.dry_run,
        )  # fmt: skip
        self.tracker = PendingTracker(cfg.settle_seconds, clock=clock, wall_clock=wall_clock)
        self._clock = clock
        self._wall = wall_clock
        # A sentinel present at startup must stay present, otherwise the source counts as unmounted.
        self._sentinels = {s: os.path.exists(os.path.join(s, SENTINEL)) for s in self.sources}
        self._health: dict[str, bool] = {}
        self._last_status: dict[str, Any] | None = None

    # -- lifecycle ------------------------------------------------------------------------------

    def startup(self) -> None:
        if not self.cfg.dry_run:
            os.makedirs(self.movies_dir, exist_ok=True)
            os.makedirs(self.shows_dir, exist_ok=True)
        log.info("found %d existing links in %s", self.linker.load(), self.cfg.library_dir)
        self.reconcile()

    def run(self, events: queue.Queue[FsEvent], stop: threading.Event) -> None:
        now = self._clock()
        next_poll, next_rescan = now, now + self.cfg.rescan_interval
        while not stop.is_set():
            timeout = min(max(0.0, min(next_poll, next_rescan) - self._clock()), 1.0)
            try:
                self._safe(self.handle, events.get(timeout=timeout))
                while True:
                    self._safe(self.handle, events.get_nowait())
            except queue.Empty:
                pass
            now = self._clock()
            if now >= next_poll:
                self._safe(self.tick)
                next_poll = now + self.cfg.poll_interval
            if now >= next_rescan:
                self._safe(self.reconcile)
                next_rescan = now + self.cfg.rescan_interval

    # -- events ---------------------------------------------------------------------------------

    def handle(self, ev: FsEvent) -> None:
        root = self.source_of(ev.path)
        if root is None:
            return
        match ev.kind:
            case EventKind.CHANGED:
                if scanner.is_candidate(self._rel(root, ev.path)):
                    self.tracker.touch(ev.path)
            case EventKind.DIR_ADDED:
                for path in scanner.walk(ev.path):
                    if scanner.is_candidate(self._rel(root, path)):
                        self.tracker.touch(path)
            case EventKind.DELETED:
                self.tracker.discard(ev.path)
                self.state.memo_delete(ev.path)
                if self.linker.links_to(ev.path) and self._healthy(root):
                    self.linker.remove_target(ev.path)
            case EventKind.DIR_DELETED:
                self.tracker.discard_under(ev.path)
                self.state.memo_delete_under(ev.path)
                if self._healthy(root):
                    self.linker.remove_under(ev.path)

    def tick(self) -> None:
        for path, st in self.tracker.poll():
            try:
                self.process(path, st)
            except TmdbError as e:
                log.warning("TMDB lookup failed for %s: %s (retrying later)", path, e)
                self.tracker.defer(path, TMDB_RETRY_SECONDS)
            except Exception:
                log.exception("failed to process %s", path)
        self._write_status()

    # -- processing a finished file -------------------------------------------------------------

    def process(self, path: str, st: os.stat_result) -> None:
        root = self.source_of(path)
        if root is None or self.linker.links_to(path):
            return  # already linked: a symlink doesn't care if the content changes later
        fp = f"{st.st_size}:{st.st_mtime_ns}"
        memo = self.state.memo_get(path)
        if (
            memo
            and memo.fingerprint == fp
            and (
                memo.status == IGNORED
                or self._wall() - memo.updated_at < self.cfg.negative_cache_seconds
            )
        ):
            return
        if st.st_size < self.cfg.min_size_bytes:
            mb = self.cfg.min_size_bytes / 1024 / 1024
            return self._memo(path, fp, IGNORED, f"smaller than {mb:g} MB")

        parsed = classify(self._rel(root, path))
        if isinstance(parsed, Ignored):
            return self._memo(path, fp, IGNORED, parsed.reason)
        placement = self._resolve(parsed)
        if placement is None:
            year = f" ({parsed.year})" if parsed.year else ""
            return self._memo(
                path, fp, UNMATCHED, f"no TMDB match for {parsed.kind} '{parsed.title}'{year}"
            )
        link = self._place(parsed, placement, path)
        if link is not None:
            self.linker.create(link, path)
        self.state.memo_delete(path)

    def _resolve(self, p: ParsedMedia) -> Placement | None:
        if p.kind is Kind.MOVIE:
            match = self.tmdb.match_movie(p.title, p.year) if self.tmdb else None
        else:
            match = self.tmdb.match_series(p.title, p.year, p.country) if self.tmdb else None
        if match is None and not self.cfg.require_tmdb_match:
            title = string.capwords(p.title) if p.title.islower() else p.title
            match = Match(None, title, p.year)
        if match is None:
            return None
        if p.kind is Kind.MOVIE:
            return Placement(match)
        if p.episodes:
            return Placement(match, p.season, p.episodes)
        if p.air_date and match.tmdb_id and self.tmdb:
            found = self.tmdb.episode_by_air_date(match.tmdb_id, p.air_date)
            if found:
                return Placement(match, found[0], (found[1],))
        return None

    def _place(self, p: ParsedMedia, pl: Placement, target: str) -> str | None:
        """First free link path for this file, or None if it is already linked there."""
        m = pl.match
        if p.kind is Kind.MOVIE:
            folder = self._folder(self.movies_dir, m)
            directory = os.path.join(self.movies_dir, folder)
            names = (naming.movie_file(folder, p, label) for label in naming.version_labels(p))
        else:
            assert pl.season is not None
            folder = self._folder(self.shows_dir, m)
            directory = os.path.join(self.shows_dir, folder, naming.season_folder(pl.season))
            names = (
                naming.episode_file(m.title, pl.season, pl.episodes, p, label)
                for label in naming.version_labels(p)
            )
        for name in names:
            link = os.path.join(directory, name)
            if self.linker.target_of(link) == target:
                return None
            if self.linker.is_free(link):
                return link
        raise AssertionError("unreachable: version_labels is infinite")

    def _folder(self, root: str, m: Match) -> str:
        """Folder name for a title, reusing an existing folder with the same TMDB id."""
        name = naming.media_folder(m.title, m.year, m.tmdb_id)
        if m.tmdb_id is None or os.path.isdir(os.path.join(root, name)):
            return name
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if (
                        entry.is_dir(follow_symlinks=False)
                        and naming.tmdb_id_of_folder(entry.name) == m.tmdb_id
                    ):
                        return entry.name
        except FileNotFoundError:
            pass
        return name

    # -- reconciliation -------------------------------------------------------------------------

    def reconcile(self) -> None:
        """Remove dangling links, forget vanished files, queue unlinked candidates."""
        started = self._clock()
        healthy = {s: self._healthy(s) for s in self.sources}

        removed = 0
        for link, target in self.linker.links().items():
            root = self.source_of(target)
            if root is None:
                log.warning("%s points outside every source (%s); removing", link, target)
            elif not healthy[root] or os.path.exists(target):
                continue
            self.linker.remove(link)
            removed += 1

        for memo in self.state.memos():
            root = self.source_of(memo.path)
            if root is None or (healthy[root] and not os.path.exists(memo.path)):
                self.state.memo_delete(memo.path)

        queued = 0
        for root in self.sources:
            for path in scanner.walk(root):
                if path in self.tracker or self.linker.links_to(path):
                    continue
                if scanner.is_candidate(self._rel(root, path)):
                    self.tracker.touch(path)
                    queued += 1
        log.info(
            "reconcile: removed %d dangling links, %d files to check (%.1fs)",
            removed, queued, self._clock() - started,
        )  # fmt: skip

    # -- helpers --------------------------------------------------------------------------------

    def source_of(self, path: str) -> str | None:
        for root in self.sources:
            if path.startswith(root + os.sep):
                return root
        return None

    @staticmethod
    def _rel(root: str, path: str) -> PurePosixPath:
        return PurePosixPath(os.path.relpath(path, root).replace(os.sep, "/"))

    def _healthy(self, root: str) -> bool:
        """False if the source looks unmounted: its sentinel vanished, or (without a sentinel)
        the root is missing or empty."""
        if self._sentinels[root]:
            ok = os.path.exists(os.path.join(root, SENTINEL))
        else:
            try:
                with os.scandir(root) as entries:
                    ok = next(entries, None) is not None
            except OSError:
                ok = False
        if self._health.get(root) != ok:
            if ok:
                log.info("source %s is available", root)
            else:
                log.warning("source %s looks unmounted or empty; keeping its links", root)
            self._health[root] = ok
        return ok

    def _memo(self, path: str, fp: str, status: str, reason: str) -> None:
        log.info("%s %s: %s", status, path, reason)
        self.state.memo_put(Memo(path, fp, status, reason, self._wall()))

    def _write_status(self) -> None:
        status = {
            "dry_run": self.cfg.dry_run,
            "links": len(self.linker.links()),
            "sources": {s: self._health.get(s, True) for s in self.sources},
            "pending": [
                {"path": p, "ready_in": None if r is None else round(r)}
                for p, r in self.tracker.snapshot()
            ],
        }
        if status == self._last_status:
            return
        self._last_status = status
        try:
            write_status(self.cfg.state_dir / "status.json", {**status, "updated_at": self._wall()})
        except OSError as e:
            log.warning("could not write status file: %s", e)

    @staticmethod
    def _safe(fn: Callable[..., None], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("unexpected error in %s", getattr(fn, "__name__", fn))
