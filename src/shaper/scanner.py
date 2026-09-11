"""Decide which files are worth looking at, and walk source trees."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import PurePath

VIDEO_EXTENSIONS = frozenset(
    {
        ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts",
        ".webm", ".mpg", ".mpeg", ".flv", ".ogv",
    }
)  # fmt: skip

# Folder names Jellyfin treats as extras; anything below them is not a main feature.
EXTRAS_DIRS = frozenset(
    {
        "behind the scenes", "deleted scenes", "interviews", "scenes", "samples", "sample",
        "shorts", "featurettes", "clips", "trailers", "extras", "theme-music", "backdrops",
    }
)  # fmt: skip


def is_candidate(rel_path: PurePath) -> bool:
    """A visible video file (path relative to its source root). Hidden files/dirs cover most
    in-progress temp files (rsync etc.); appended temp suffixes like `.part` or `.!qB` fail the
    extension check."""
    if rel_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    return not any(part.startswith(".") for part in rel_path.parts)


def in_extras_dir(rel_path: PurePath) -> bool:
    return any(part.lower() in EXTRAS_DIRS for part in rel_path.parent.parts)


def walk(root: str | os.PathLike[str]) -> Iterator[str]:
    """Yield candidate video file paths below root, not following symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                yield os.path.join(dirpath, name)
