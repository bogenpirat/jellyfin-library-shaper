"""The symlinks under the library directory.

The library tree is the source of truth: `load()` rebuilds the link index from disk. Only symlinks
are ever created or deleted; regular files are never touched.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections import defaultdict
from collections.abc import Iterable

log = logging.getLogger(__name__)


class Linker:
    def __init__(
        self, library_dir: str, prune_stops: Iterable[str] = (), *, dry_run: bool = False
    ) -> None:
        self.library_dir = os.path.normpath(library_dir)
        self._stops = {os.path.normpath(p) for p in prune_stops} | {self.library_dir}
        self.dry_run = dry_run
        self._by_link: dict[str, str] = {}
        self._by_target: defaultdict[str, set[str]] = defaultdict(set)

    # -- index ----------------------------------------------------------------------------------

    def load(self) -> int:
        self._by_link.clear()
        self._by_target.clear()
        for dirpath, dirnames, filenames in os.walk(self.library_dir):
            for name in (*filenames, *dirnames):
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    self._index(path, os.readlink(path))
        return len(self._by_link)

    def links(self) -> dict[str, str]:
        """Snapshot of link path -> target."""
        return dict(self._by_link)

    def links_to(self, target: str) -> set[str]:
        return set(self._by_target.get(target, ()))

    def target_of(self, link_path: str) -> str | None:
        return self._by_link.get(link_path)

    def is_free(self, link_path: str) -> bool:
        return link_path not in self._by_link and not os.path.lexists(link_path)

    # -- mutations ------------------------------------------------------------------------------

    def create(self, link_path: str, target: str) -> None:
        log.info("%slink %s -> %s", self._prefix, link_path, target)
        if not self.dry_run:
            parent = os.path.dirname(link_path)
            os.makedirs(parent, exist_ok=True)
            tmp = os.path.join(parent, f".{os.path.basename(link_path)}.shaper-tmp")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            os.symlink(target, tmp)
            os.replace(tmp, link_path)
        self._index(link_path, target)

    def remove(self, link_path: str) -> None:
        target = self._by_link.pop(link_path, None)
        if target is not None:
            links = self._by_target[target]
            links.discard(link_path)
            if not links:
                del self._by_target[target]
        log.info("%sunlink %s (target %s)", self._prefix, link_path, target)
        if self.dry_run:
            return
        if os.path.islink(link_path):
            os.unlink(link_path)
        self._prune(os.path.dirname(link_path))

    def remove_target(self, target: str) -> int:
        links = self.links_to(target)
        for link in links:
            self.remove(link)
        return len(links)

    def remove_under(self, directory: str) -> int:
        prefix = directory.rstrip("/") + "/"
        return sum(self.remove_target(t) for t in list(self._by_target) if t.startswith(prefix))

    # -- helpers --------------------------------------------------------------------------------

    @property
    def _prefix(self) -> str:
        return "[dry-run] " if self.dry_run else ""

    def _index(self, link_path: str, target: str) -> None:
        self._by_link[link_path] = target
        self._by_target[target].add(link_path)

    def _prune(self, directory: str) -> None:
        """Remove now-empty directories up to (not including) a stop directory."""
        directory = os.path.normpath(directory)
        while directory not in self._stops and directory.startswith(self.library_dir + os.sep):
            try:
                os.rmdir(directory)
            except OSError:
                return
            directory = os.path.dirname(directory)
