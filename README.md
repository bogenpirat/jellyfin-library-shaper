# jellyfin-library-shaper

Watches one or more "dump" folders full of movies and TV episodes in arbitrary folder structures and
maintains a separate, Jellyfin-shaped tree of **symlinks** pointing at them:

```
/media/library
├── Movies
│   └── The Matrix (1999) [tmdbid-603]
│       └── The Matrix (1999) [tmdbid-603].mkv -> /media/dump/stuff/the.matrix.1999.1080p.mkv
└── Shows
    └── Some Show (2011) [tmdbid-1399]
        └── Season 01
            └── Some Show S01E02.mkv -> /media/downloads/x/Some.Show.S01E02.720p.mkv
```

The dump folders are never modified (they're mounted read-only).

## How it works

- **Only finished files are linked.** Events never create links directly. A file is linked once its
  size, mtime, ctime and permissions have been unchanged for `SETTLE_SECONDS` (default 120) **and**
  the service can actually read it. Slow writers that open and close the file repeatedly, or keep it
  unreadable (e.g. `chmod 000`) until done, are handled. Files a rescan finds long after they were
  written are linked straight away.
- **Movie or episode?** The path relative to the source root (parent folders included) is parsed with
  [guessit](https://github.com/guessit-io/guessit). Anything with a season/episode number or an air
  date is an episode; everything else is a movie candidate.
- **Ignoring non-media.** The following are skipped:
  - non-video extensions, and hidden or temporary files (`.part`, `.!qB`, rsync temp files)
  - samples and trailers, and anything in an extras folder (`Featurettes/`, `Extras/`, …)
  - camera/phone recordings (`IMG_1234.mp4`)
  - files smaller than `MIN_SIZE_MB`
  - anything TMDB can't identify

  The TMDB check is the real gate: guessit happily turns a home video into a "movie", but it won't
  survive a title match (≥ 85% similar, year within ±1).
- **Naming** follows the [Jellyfin docs](https://jellyfin.org/docs/general/server/media/movies/). A
  second copy of the same movie or episode becomes a Jellyfin *version*
  (`… - 1080p.mkv`, `… - 2160p.mkv`), and split movies become `-cd1`, `-cd2`. All episodes of a
  series share one folder, keyed by TMDB id.
- **Deletions.** When a source file (or a whole directory) is deleted or moved away, its links go too.
  Folders left empty are cleaned up.
- **Self-healing.** On startup and every `RESCAN_INTERVAL` (default 15 min), the service does a full
  pass. It removes dangling links, queues anything unlinked, and catches up on events missed while
  the service was down. The link tree on disk is the source of truth; no link database can drift.

### Safety

- **Unmounted drives don't wipe your library.** Suppose a source looks unmounted: its folder is empty
  or missing, or a `.shaper-sentinel` file that was there at startup has vanished. Then its links are
  kept and a warning is logged. Removing them would also make Jellyfin forget watch history. Put an
  empty `.shaper-sentinel` in the root of every source on a removable or network disk. This is also
  the more precise check if a source root may legitimately become empty.
- **Only symlinks are ever deleted.** Regular files in the library are never touched. The library
  directory belongs to the shaper, though: symlinks in it that point outside every configured source
  are removed.

## Setup

1. Get a TMDB API key (v3 key or v4 read access token): <https://www.themoviedb.org/settings/api>.
2. `cp .env.example .env` and fill in `TMDB_API_KEY`.
3. Edit the volume paths in `compose.yaml`. **The container paths must be identical in the Jellyfin
   container**, because symlink targets are absolute paths. For example, if the shaper sees
   `/media/dump`, so must Jellyfin.
4. Run the shaper with the same `user:` UID/GID as Jellyfin, so "readable to the shaper" means
   "readable to Jellyfin".
5. First run with `DRY_RUN=true` (the default in `.env.example`). Check the logs and
   `docker compose exec shaper python -m shaper report`, then set `DRY_RUN=false`.
6. In Jellyfin, add two libraries:
   - **Movies** (type *Movies*) → `/media/library/Movies`
   - **Shows** (type *Shows*) → `/media/library/Shows`

   Enable *real-time monitoring* on both. Mount the library read-only in Jellyfin (see the example in
   `compose.yaml`), so Jellyfin can't save artwork or `.nfo` files into the managed folders.

```sh
docker compose up -d --build
docker compose logs -f shaper
docker compose exec shaper python -m shaper report   # pending / unmatched / ignored files, and why
```

## Configuration

| Variable               | Default    | Meaning |
|------------------------|------------|---------|
| `SOURCE_DIRS`          | (required) | Comma-separated container paths to watch. Must not overlap each other or the library. |
| `LIBRARY_DIR`          | (required) | Where the Jellyfin tree of symlinks is maintained. |
| `TMDB_API_KEY`         | (required\*) | TMDB v3 key or v4 read access token. |
| `REQUIRE_TMDB_MATCH`   | `true`     | \*If `false`, unmatched files are linked with their parsed title and no key is needed. Expect junk. |
| `TMDB_LANGUAGE`        | `en-US`    | Language of titles used for folder names. |
| `SETTLE_SECONDS`       | `120`      | How long a file must be unchanged (and readable) before it's linked. |
| `MIN_SIZE_MB`          | `40`       | Smaller video files are ignored. |
| `RESCAN_INTERVAL`      | `900`      | Seconds between full reconcile passes. |
| `POLL_INTERVAL`        | `5`        | Seconds between checks of pending files. |
| `NEGATIVE_CACHE_HOURS` | `24`       | How long an unmatched file waits before TMDB is asked again. |
| `DRY_RUN`              | `false`    | Log link/unlink actions without writing anything. |
| `WATCH_MODE`           | `inotify`  | `poll` for filesystems without inotify (the rescan is a backstop either way). |
| `MOVIES_SUBDIR` / `SHOWS_SUBDIR` | `Movies` / `Shows` | Subfolders of `LIBRARY_DIR`. |
| `STATE_DIR`            | `/config`  | TMDB cache, ignore/unmatched memo (`state.db`) and `status.json`. |
| `LOG_LEVEL`            | `INFO`     | |

## Limitations

- **Absolute-numbered anime episodes** (`Show - 112.mkv`) go to `Season 01`.
- **Date-named episodes** (`Show.2024.03.14.mkv`) are looked up by air date on TMDB. They are skipped
  if TMDB doesn't list the episode yet; the file is retried after `NEGATIVE_CACHE_HOURS`.
- **Only video files are linked.** Subtitles, `.nfo` files, extras, ISOs and DVD/Blu-ray folder
  structures are not.
- **A writer that stalls for longer than `SETTLE_SECONDS` mid-file** gets linked early. The link is
  still correct once the file is complete, because a symlink always shows the current content.
- **Very deep dumps** may need a higher `fs.inotify.max_user_watches` sysctl on the host. There is one
  watch per directory; the default is usually plenty.

## Development

The service targets Linux (inotify, POSIX symlinks, ctime semantics). On any OS:

```sh
uv sync
uv run pytest                  # portable tests; Linux-only tests are skipped off Linux
uv run ruff check src tests && uv run ruff format --check src tests
```

The full suite, including symlink and real-inotify end-to-end tests, runs in a container:

```sh
docker compose -f compose.test.yaml run --rm --build test
```

These tests use temporary directories inside the container. Docker Desktop bind mounts from Windows
don't deliver inotify events.
