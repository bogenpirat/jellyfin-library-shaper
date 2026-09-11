"""Command line entry point: `shaper run` (default) and `shaper report`."""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
import time
from collections import defaultdict

from .config import Config, ConfigError
from .service import Service
from .state import IGNORED, UNMATCHED, State, read_status
from .tmdb import TmdbClient
from .watcher import FsEvent, start_observer

log = logging.getLogger("shaper")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shaper", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="watch sources and maintain the library (default)")
    sub.add_parser("report", help="show pending, ignored and unmatched files")
    args = parser.parse_args(argv)

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if args.command == "report":
        return report(cfg)
    return run(cfg)


def run(cfg: Config) -> int:
    try:
        cfg.validate()
    except ConfigError as e:
        log.error("configuration error: %s", e)
        return 2
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    state = State(cfg.state_dir / "state.db")
    tmdb = (
        TmdbClient(
            cfg.tmdb_api_key,
            state,
            language=cfg.tmdb_language,
            negative_ttl=cfg.negative_cache_seconds,
        )  # fmt: skip
        if cfg.tmdb_api_key
        else None
    )
    service = Service(cfg, state, tmdb)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    log.info(
        "sources=%s library=%s settle=%gs dry_run=%s",
        ",".join(service.sources), cfg.library_dir, cfg.settle_seconds, cfg.dry_run,
    )  # fmt: skip
    events: queue.Queue[FsEvent] = queue.Queue()
    # Watch before the startup scan so nothing written during the scan slips through.
    observer = start_observer(service.sources, cfg.watch_mode, events)
    try:
        service.startup()
        service.run(events, stop)
    finally:
        log.info("shutting down")
        observer.stop()
        observer.join(timeout=10)
        if tmdb:
            tmdb.close()
        state.close()
    return 0


def report(cfg: Config) -> int:
    sources = [str(s).rstrip("/") for s in cfg.source_dirs]

    def source_of(path: str) -> str:
        return next((s for s in sources if path.startswith(s + "/")), "(not a configured source)")

    status = read_status(cfg.state_dir / "status.json")
    if status is None:
        print("No status file yet; is the service running?")
    else:
        updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(status["updated_at"]))
        print(f"Status as of {updated}: {status['links']} links, dry run: {status['dry_run']}")
        for source, healthy in status["sources"].items():
            print(f"  source {source}: {'ok' if healthy else 'UNMOUNTED OR EMPTY'}")
        pending = status["pending"]
        print(f"\nPending ({len(pending)}):")
        for item in pending:
            eta = item["ready_in"]
            if eta is None:
                when = "not checked yet"
            else:
                when = f"settled in ~{eta}s" if eta else "checking"
            print(f"  {item['path']}  [{when}]")

    db = cfg.state_dir / "state.db"
    memos = State(db, readonly=True).memos() if db.exists() else []
    for status_name, title in ((UNMATCHED, "Unmatched"), (IGNORED, "Ignored")):
        grouped: defaultdict[str, list[str]] = defaultdict(list)
        for m in memos:
            if m.status == status_name:
                grouped[source_of(m.path)].append(f"  {m.path}\n      {m.reason}")
        total = sum(map(len, grouped.values()))
        print(f"\n{title} ({total}):")
        for source, lines in sorted(grouped.items()):
            print(f" {source}")
            print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
