import os
from pathlib import Path

import pytest

from shaper.linker import Linker

pytestmark = pytest.mark.linux


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    (tmp_path / "lib" / "Movies").mkdir(parents=True)
    return tmp_path / "lib"


def make(lib: Path, **kw: bool) -> Linker:
    return Linker(str(lib), [str(lib / "Movies")], **kw)


def test_create_and_remove_prunes_empty_dirs(lib: Path) -> None:
    linker = make(lib)
    link = str(lib / "Movies" / "X (2000)" / "X (2000).mkv")
    linker.create(link, "/src/x.mkv")
    assert os.readlink(link) == "/src/x.mkv"
    assert linker.links_to("/src/x.mkv") == {link}
    assert not linker.is_free(link)
    assert linker.remove_target("/src/x.mkv") == 1
    assert not os.path.lexists(link)
    assert not (lib / "Movies" / "X (2000)").exists()
    assert (lib / "Movies").is_dir()  # stop directory survives


def test_prune_stops_at_non_empty_dir(lib: Path) -> None:
    linker = make(lib)
    a = str(lib / "Movies" / "X" / "a.mkv")
    b = str(lib / "Movies" / "X" / "b.mkv")
    linker.create(a, "/src/a.mkv")
    linker.create(b, "/src/b.mkv")
    linker.remove(a)
    assert (lib / "Movies" / "X").is_dir()
    assert os.path.islink(b)


def test_load_rebuilds_index_from_disk(lib: Path) -> None:
    make(lib).create(str(lib / "Movies" / "X" / "x.mkv"), "/src/x.mkv")
    linker = make(lib)
    assert linker.load() == 1
    assert linker.links() == {str(lib / "Movies" / "X" / "x.mkv"): "/src/x.mkv"}


def test_never_deletes_regular_files(lib: Path) -> None:
    real = lib / "Movies" / "X" / "poster.jpg"
    real.parent.mkdir()
    real.write_bytes(b"img")
    make(lib).remove(str(real))
    assert real.read_bytes() == b"img"


def test_remove_under(lib: Path) -> None:
    linker = make(lib)
    linker.create(str(lib / "Movies" / "A" / "a.mkv"), "/src/dir/a.mkv")
    linker.create(str(lib / "Movies" / "B" / "b.mkv"), "/src/dir/sub/b.mkv")
    linker.create(str(lib / "Movies" / "C" / "c.mkv"), "/src/dirx/c.mkv")
    assert linker.remove_under("/src/dir") == 2
    assert list(linker.links().values()) == ["/src/dirx/c.mkv"]


def test_dry_run_touches_nothing(lib: Path) -> None:
    linker = make(lib, dry_run=True)
    link = str(lib / "Movies" / "X" / "x.mkv")
    linker.create(link, "/src/x.mkv")
    assert not os.path.lexists(link)
    assert linker.links_to("/src/x.mkv") == {link}
    linker.remove(link)
    assert linker.links() == {}
