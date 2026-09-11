"""Translate watchdog events into a few service-level events on a queue.

Opened / closed-without-write events are dropped: our own read probe (and Jellyfin reading files)
produces them and must not restart the settle window.
"""

from __future__ import annotations

import os
import queue
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver


class EventKind(Enum):
    CHANGED = auto()  # a file appeared or was written to
    DELETED = auto()  # a file disappeared
    DIR_ADDED = auto()  # a directory appeared (created or moved in): scan it
    DIR_DELETED = auto()  # a directory disappeared (deleted or moved away)


@dataclass(frozen=True, slots=True)
class FsEvent:
    kind: EventKind
    path: str


def translate(event: FileSystemEvent) -> list[FsEvent]:
    src = os.fsdecode(event.src_path)
    dest = os.fsdecode(event.dest_path) if event.dest_path else ""
    is_dir = event.is_directory
    match event.event_type:
        case "moved" if is_dir:
            return [FsEvent(EventKind.DIR_DELETED, src), FsEvent(EventKind.DIR_ADDED, dest)]
        case "moved":
            return [FsEvent(EventKind.DELETED, src), FsEvent(EventKind.CHANGED, dest)]
        case "created" if is_dir:
            return [FsEvent(EventKind.DIR_ADDED, src)]
        case "deleted" if is_dir:
            return [FsEvent(EventKind.DIR_DELETED, src)]
        case "created" | "modified" | "closed" if not is_dir:
            return [FsEvent(EventKind.CHANGED, src)]
        case "deleted":
            return [FsEvent(EventKind.DELETED, src)]
    return []


class _Handler(FileSystemEventHandler):
    def __init__(self, events: queue.Queue[FsEvent]) -> None:
        self._events = events

    def on_any_event(self, event: FileSystemEvent) -> None:
        for e in translate(event):
            self._events.put(e)


def start_observer(
    roots: Iterable[str], mode: str, events: queue.Queue[FsEvent], *, poll_timeout: float = 30.0
) -> BaseObserver:
    observer: BaseObserver = PollingObserver(timeout=poll_timeout) if mode == "poll" else Observer()
    handler = _Handler(events)
    for root in roots:
        observer.schedule(handler, root, recursive=True)
    observer.start()
    return observer
