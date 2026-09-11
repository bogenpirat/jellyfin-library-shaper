"""Environment-driven configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class ConfigError(Exception):
    pass


def _bool(value: str) -> bool:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError(f"not a boolean: {value!r}")


@dataclass(frozen=True)
class Config:
    source_dirs: tuple[Path, ...]
    library_dir: Path
    state_dir: Path = Path("/config")
    movies_subdir: str = "Movies"
    shows_subdir: str = "Shows"
    settle_seconds: float = 120.0
    poll_interval: float = 5.0
    rescan_interval: float = 900.0
    min_size_bytes: int = 40 * 1024 * 1024
    tmdb_api_key: str | None = None
    tmdb_language: str = "en-US"
    require_tmdb_match: bool = True
    negative_cache_seconds: float = 24 * 3600.0
    dry_run: bool = False
    watch_mode: Literal["inotify", "poll"] = "inotify"
    log_level: str = "INFO"

    @property
    def movies_dir(self) -> Path:
        return self.library_dir / self.movies_subdir

    @property
    def shows_dir(self) -> Path:
        return self.library_dir / self.shows_subdir

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        raw_sources = env.get("SOURCE_DIRS") or env.get("SOURCE_DIR") or ""
        sources = tuple(Path(s.strip()) for s in raw_sources.split(",") if s.strip())
        if not sources:
            raise ConfigError("SOURCE_DIRS is required (comma-separated container paths)")
        library = env.get("LIBRARY_DIR", "").strip()
        if not library:
            raise ConfigError("LIBRARY_DIR is required")
        watch_mode = env.get("WATCH_MODE", "inotify").strip().lower()
        if watch_mode not in ("inotify", "poll"):
            raise ConfigError(f"WATCH_MODE must be 'inotify' or 'poll', got {watch_mode!r}")
        try:
            return cls(
                source_dirs=sources,
                library_dir=Path(library),
                state_dir=Path(env.get("STATE_DIR", "/config")),
                movies_subdir=env.get("MOVIES_SUBDIR", "Movies"),
                shows_subdir=env.get("SHOWS_SUBDIR", "Shows"),
                settle_seconds=float(env.get("SETTLE_SECONDS", 120)),
                poll_interval=float(env.get("POLL_INTERVAL", 5)),
                rescan_interval=float(env.get("RESCAN_INTERVAL", 900)),
                min_size_bytes=int(float(env.get("MIN_SIZE_MB", 40)) * 1024 * 1024),
                tmdb_api_key=env.get("TMDB_API_KEY", "").strip() or None,
                tmdb_language=env.get("TMDB_LANGUAGE", "en-US"),
                require_tmdb_match=_bool(env.get("REQUIRE_TMDB_MATCH", "true")),
                negative_cache_seconds=float(env.get("NEGATIVE_CACHE_HOURS", 24)) * 3600,
                dry_run=_bool(env.get("DRY_RUN", "false")),
                watch_mode=watch_mode,  # type: ignore[arg-type]
                log_level=env.get("LOG_LEVEL", "INFO").upper(),
            )
        except ValueError as e:
            raise ConfigError(str(e)) from e

    def validate(self) -> None:
        """Check paths exist and don't overlap. Raises ConfigError."""
        if self.require_tmdb_match and not self.tmdb_api_key:
            raise ConfigError("TMDB_API_KEY is required unless REQUIRE_TMDB_MATCH=false")
        for d in (*self.source_dirs, self.library_dir):
            if not d.is_absolute():
                raise ConfigError(f"path must be absolute: {d}")
        for s in self.source_dirs:
            if not s.is_dir():
                raise ConfigError(f"source directory does not exist: {s}")
        if not self.library_dir.is_dir():
            raise ConfigError(f"library directory does not exist: {self.library_dir}")

        resolved = [(s, s.resolve()) for s in self.source_dirs]
        library = self.library_dir.resolve()
        for i, (a, ra) in enumerate(resolved):
            if ra == library or ra.is_relative_to(library) or library.is_relative_to(ra):
                raise ConfigError(f"source {a} and LIBRARY_DIR {self.library_dir} overlap")
            for b, rb in resolved[i + 1 :]:
                if ra == rb or ra.is_relative_to(rb) or rb.is_relative_to(ra):
                    raise ConfigError(f"sources {a} and {b} overlap")
