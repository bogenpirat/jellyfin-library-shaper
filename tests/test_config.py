from pathlib import Path

import pytest

from shaper.config import Config, ConfigError


def test_from_env() -> None:
    cfg = Config.from_env({
        "SOURCE_DIRS": " /media/dump , /media/downloads ,",
        "LIBRARY_DIR": "/media/library",
        "MIN_SIZE_MB": "1.5",
        "DRY_RUN": "yes",
        "NEGATIVE_CACHE_HOURS": "2",
    })  # fmt: skip
    assert cfg.source_dirs == (Path("/media/dump"), Path("/media/downloads"))
    assert cfg.min_size_bytes == 1_572_864
    assert cfg.dry_run is True
    assert cfg.negative_cache_seconds == 7200
    assert cfg.movies_dir == Path("/media/library/Movies")


@pytest.mark.parametrize(
    "env",
    [
        {"LIBRARY_DIR": "/lib"},
        {"SOURCE_DIRS": "/a"},
        {"SOURCE_DIRS": "/a", "LIBRARY_DIR": "/lib", "DRY_RUN": "maybe"},
        {"SOURCE_DIRS": "/a", "LIBRARY_DIR": "/lib", "WATCH_MODE": "fanotify"},
        {"SOURCE_DIRS": "/a", "LIBRARY_DIR": "/lib", "SETTLE_SECONDS": "soon"},
    ],
)
def test_from_env_errors(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Config.from_env(env)


def _cfg(tmp_path: Path, sources: list[str], library: str = "lib", **kw: object) -> Config:
    for d in (*sources, library):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    kw.setdefault("tmdb_api_key", "key")
    return Config(
        source_dirs=tuple(tmp_path / s for s in sources), library_dir=tmp_path / library,
        **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def test_validate_ok(tmp_path: Path) -> None:
    _cfg(tmp_path, ["dump", "downloads"]).validate()


@pytest.mark.parametrize(
    ("sources", "library"),
    [
        (["dump", "dump/sub"], "lib"),
        (["dump"], "dump/lib"),
        (["lib/dump"], "lib"),
        (["dump", "dump"], "lib"),
    ],
)
def test_validate_rejects_overlap(tmp_path: Path, sources: list[str], library: str) -> None:
    with pytest.raises(ConfigError, match="overlap"):
        _cfg(tmp_path, sources, library).validate()


def test_validate_requires_key_when_matching_is_required(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ["dump"], tmdb_api_key=None)
    with pytest.raises(ConfigError, match="TMDB_API_KEY"):
        cfg.validate()


def test_validate_missing_source(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ["dump"])
    cfg = Config(source_dirs=(tmp_path / "nope",), library_dir=cfg.library_dir, tmdb_api_key="k")
    with pytest.raises(ConfigError, match="does not exist"):
        cfg.validate()
