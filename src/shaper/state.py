"""Persistent state: TMDB response cache, per-file memo of ignored/unmatched results, status file.

Links themselves are never recorded here; the library tree is the source of truth for those.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tmdb_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_memo (
    path        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""

IGNORED = "ignored"
UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class Memo:
    path: str
    fingerprint: str
    status: str
    reason: str
    updated_at: float


class State:
    def __init__(self, db_path: str | Path, *, readonly: bool = False) -> None:
        if readonly:
            self._db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None)
        else:
            # The service is single-threaded, but not necessarily the thread that opened the db.
            self._db = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
            if str(db_path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- TMDB cache -----------------------------------------------------------------------------

    def cache_get(self, key: str) -> tuple[Any, float] | None:
        """(payload, fetched_at); payload None is a cached negative result."""
        row = self._db.execute(
            "SELECT payload, fetched_at FROM tmdb_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return (json.loads(row[0]) if row[0] is not None else None, row[1])

    def cache_put(self, key: str, payload: Any, now: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO tmdb_cache (key, payload, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(payload) if payload is not None else None, now),
        )

    # -- file memo ------------------------------------------------------------------------------

    def memo_get(self, path: str) -> Memo | None:
        row = self._db.execute(
            "SELECT path, fingerprint, status, reason, updated_at FROM file_memo WHERE path = ?",
            (path,),
        ).fetchone()
        return Memo(*row) if row else None

    def memo_put(self, memo: Memo) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO file_memo VALUES (?, ?, ?, ?, ?)",
            (memo.path, memo.fingerprint, memo.status, memo.reason, memo.updated_at),
        )

    def memo_delete(self, path: str) -> None:
        self._db.execute("DELETE FROM file_memo WHERE path = ?", (path,))

    def memo_delete_under(self, directory: str) -> None:
        prefix = directory.rstrip("/") + "/"
        self._db.execute(
            "DELETE FROM file_memo WHERE substr(path, 1, ?) = ?", (len(prefix), prefix)
        )

    def memos(self) -> list[Memo]:
        rows = self._db.execute(
            "SELECT path, fingerprint, status, reason, updated_at FROM file_memo ORDER BY path"
        ).fetchall()
        return [Memo(*r) for r in rows]


def write_status(path: Path, status: dict[str, Any]) -> None:
    """Atomically replace the JSON status file read by `shaper report`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".status-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
