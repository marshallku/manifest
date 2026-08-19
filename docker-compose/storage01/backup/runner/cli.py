"""Entry point: lock, run every job, snapshot only if all of them worked."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import VERSION, config as config_module, jobs, manifest, snapshot
from .errors import BackupError, ConfigError

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    try:
        cfg = config_module.load(args.config)
    except (ConfigError, OSError) as error:
        print(f"config: {error}", file=sys.stderr)
        return EXIT_CONFIG

    selected = _select(cfg, args.only)
    if not selected:
        print(f"no job matches {args.only}", file=sys.stderr)
        return EXIT_CONFIG

    if args.list:
        for job in selected:
            print(f"{job.name}\t{job.source}\t{job.bundle}")
        return EXIT_OK

    if args.dry_run:
        # A dry run neither writes nor conflicts, so it must work from a
        # laptop that has never mounted the destination.
        return _run(cfg, selected, args, log)

    if not cfg.root.is_dir():
        print(f"destination.root does not exist: {cfg.root}", file=sys.stderr)
        return EXIT_CONFIG

    lock = cfg.root / ".lock"
    with lock.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another run holds {lock}", file=sys.stderr)
            return EXIT_LOCKED
        return _run(cfg, selected, args, log)


def _run(cfg, selected, args, log) -> int:
    started_at = jobs.now()
    log(f"backup runner {VERSION} -> {cfg.root}" + ("  [dry run]" if args.dry_run else ""))

    reports = [jobs.run_job(cfg, job, log=log, dry_run=args.dry_run) for job in selected]
    failed = [r.name for r in reports if not r.ok]

    snapshot_result: dict | None = None
    snapshot_error: str | None = None
    if failed:
        log(f"not snapshotting: {len(failed)} job(s) failed ({', '.join(failed)})")
    elif args.no_snapshot:
        log("not snapshotting: --no-snapshot")
    elif args.only:
        # A partial run is not a state worth freezing as if it were a full one.
        log("not snapshotting: this was a partial run (--only)")
    else:
        try:
            snapshot_result = snapshot.take(cfg, log=log, dry_run=args.dry_run)
        except BackupError as error:
            snapshot_error = str(error)
            log(f"snapshot failed: {error}")

    summary = {
        "schema": manifest.SCHEMA,
        "runner_version": VERSION,
        "started_at": started_at,
        "finished_at": jobs.now(),
        "dry_run": args.dry_run,
        "partial": bool(args.only),
        "jobs": [asdict(r) for r in reports],
        "failed": failed,
        "snapshot": snapshot_result,
        "snapshot_error": snapshot_error,
    }

    if not args.dry_run:
        manifest.write(cfg.root / "run.json", summary)
    if args.json:
        print(json.dumps(summary, indent=2))

    log(f"{len(reports) - len(failed)}/{len(reports)} job(s) ok")
    return EXIT_JOB_FAILED if failed or snapshot_error else EXIT_OK


def _select(cfg, only: list[str]):
    if not only:
        return list(cfg.jobs)
    wanted = set(only)
    return [job for job in cfg.jobs if job.name in wanted]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="backup", description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.yaml")
    parser.add_argument("--only", action="append", default=[], metavar="JOB",
                        help="run only this job (repeatable); implies --no-snapshot")
    parser.add_argument("--dry-run", action="store_true", help="print every command, change nothing")
    parser.add_argument("--no-snapshot", action="store_true", help="collect but do not snapshot")
    parser.add_argument("--list", action="store_true", help="list jobs and exit")
    parser.add_argument("--json", action="store_true", help="print the run summary on stdout")
    parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")
    return parser.parse_args(argv)
