"""Run one job: check its assumptions, pull its files, take its dumps.

Jobs are isolated from each other — one failure does not stop the rest — but
it does stop the snapshot, so a partial collection is never preserved as if it
were whole.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from . import dumps, execute, manifest, source as transport, sqlite_dump
from .config import Config, DumpSpec, Job, PathSpec
from .errors import JobError

Log = Callable[[str], None]

# States in which no process is holding the container's data directory open.
# Deliberately an allowlist: `restarting`, `paused` and `removing` all still
# mean a live datadir, and a state Docker adds later should fail closed rather
# than quietly pass a check whose entire purpose is to fail.
DORMANT_STATES = frozenset({"created", "exited", "dead"})


@dataclass
class JobReport:
    name: str
    status: str = "ok"
    started_at: str = ""
    finished_at: str = ""
    error: str | None = None
    artifacts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_job(config: Config, job: Job, *, log: Log, dry_run: bool = False) -> JobReport:
    src = config.sources[job.source]
    report = JobReport(name=job.name, started_at=now())

    try:
        _check_precondition(config, job, log=log, dry_run=dry_run)
        for spec in job.paths:
            report.artifacts.append(_pull_paths(config, job, spec, log=log, dry_run=dry_run))
        for spec in job.dumps:
            report.artifacts.append(_take_dump(config, job, spec, log=log, dry_run=dry_run))
    except Exception as error:  # noqa: BLE001 — one job's failure is data, not a crash
        report.status = "failed"
        report.error = str(error)
        log(f"  ✗ {job.name}: {error}")
    else:
        log(f"  ✓ {job.name}")

    report.finished_at = now()

    if not dry_run:
        manifest.write(
            config.root / job.bundle / "manifest.json",
            manifest.for_job(
                config,
                job,
                status=report.status,
                started_at=report.started_at,
                finished_at=report.finished_at,
                artifacts=report.artifacts,
                error=report.error,
            ),
        )
    return report


def _check_precondition(config: Config, job: Job, *, log: Log, dry_run: bool) -> None:
    container = job.precondition.get("container_not_running")
    if not container:
        return
    src = config.sources[job.source]
    argv = transport.container_state_argv(src, container)
    log(f"    precondition: {container} must not be running")
    if dry_run:
        log(f"      $ {shlex.join(argv)}")
        return
    result = execute.run(argv, timeout=60)
    if result.returncode != 0:
        # An unreachable host or a dead docker daemon is not evidence that the
        # container is stopped, and treating it as such would green-light an
        # rsync of a live datadir.
        raise JobError(
            f"precondition could not be checked (exit {result.returncode}): {result.stderr.strip()}"
        )
    state = result.stdout.strip()
    if state and state not in DORMANT_STATES:
        raise JobError(
            f"precondition failed: container {container!r} is {state!r}, so its data directory "
            "may be live and copying it with rsync would produce an inconsistent database. "
            "Convert this job to a logical dump."
        )


def _pull_paths(config: Config, job: Job, spec: PathSpec, *, log: Log, dry_run: bool) -> dict:
    src = config.sources[job.source]
    dest = config.root / spec.dest
    argv = transport.rsync_argv(src, src=spec.src, dest=str(dest), exclude=spec.exclude, dry_run=dry_run)

    log(f"    paths: {src.location}:{spec.src} -> {spec.dest}")
    if dry_run:
        log(f"      $ {shlex.join(argv)}")
        return {"kind": "paths", "from": spec.src, "to": spec.dest, "dry_run": True}

    dest.mkdir(parents=True, exist_ok=True)
    execute.check(argv, f"rsync of {spec.src}", timeout=21600)
    size, files = execute.tree_size(dest)
    if files == 0:
        # Not an error — blog-backend really does hold nothing but its excluded
        # datadir — but an empty result and a broken exclude look identical in
        # the manifest, so say it out loud rather than leaving it to be noticed
        # at restore time.
        log(f"      ! {spec.dest} is empty; check whether the excludes are right")

    return {
        "kind": "paths",
        "from": spec.src,
        "to": spec.dest,
        "exclude": list(spec.exclude),
        "fake_super": src.mode == "ssh",
        "bytes": size,
        "files": files,
        "restore": manifest.restore_paths(src, spec, config.root),
    }


def _take_dump(config: Config, job: Job, spec: DumpSpec, *, log: Log, dry_run: bool) -> dict:
    if spec.engine == "sqlite":
        return _take_sqlite_dump(config, job, spec, log=log, dry_run=dry_run)

    src = config.sources[job.source]
    command = dumps.build(spec)
    if src.mode == "kubectl":
        argv = transport.exec_in_pod(src, spec.pod or "", spec.container, command.snippet)
        probe = transport.exec_in_pod(src, spec.pod or "", spec.container, dumps.version_snippet(command.tool))
        where = spec.pod
    else:
        argv = transport.exec_in_container(src, spec.container or "", command.snippet)
        probe = transport.exec_in_container(src, spec.container or "", dumps.version_snippet(command.tool))
        where = spec.container

    log(f"    dump: {command.tool} in {where} -> {spec.dest}" + ("  (+gzip)" if spec.compress else ""))
    if dry_run:
        log(f"      $ {shlex.join(argv)}")
        return {"kind": "dump", "to": spec.dest, "engine": spec.engine, "dry_run": True}

    probed = execute.run(probe, timeout=60)
    version = probed.stdout.strip() if probed.ok else None
    dest = config.root / spec.dest
    size = execute.stream_to_file(argv, dest, compress=spec.compress)

    return {
        "kind": "dump",
        "to": spec.dest,
        "engine": spec.engine,
        "engine_version": version or None,
        "container": spec.container,
        "pod": spec.pod,
        "database": spec.database,
        "args": list(spec.args),
        "compressed_by_runner": spec.compress,
        "bytes": size,
        "restore": manifest.restore_dump(src, spec, config.root),
    }


def _take_sqlite_dump(config: Config, job: Job, spec: DumpSpec, *, log: Log, dry_run: bool) -> dict:
    src = config.sources[job.source]
    argv = transport.python_program(src, [spec.path or "", spec.tmp or ""])

    log(f"    dump: sqlite {spec.path} -> {spec.dest}  (VACUUM INTO via the source's python3)")
    if dry_run:
        log(f"      $ {shlex.join(argv)} < <vacuum-into program>")
        return {"kind": "dump", "to": spec.dest, "engine": "sqlite", "dry_run": True}

    dest = config.root / spec.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / (dest.name + execute.PART_SUFFIX)

    # Deliberately OUTSIDE the cleanup below. If this fails the temp file is
    # not ours — most importantly when it failed *because* a leftover from a
    # killed run is sitting there. Deleting it here would destroy the evidence
    # and turn a condition that demands a look into one that merely fails once.
    result = execute.check(
        argv, f"VACUUM INTO on {spec.path}", input=sqlite_dump.PROGRAM.encode(), timeout=3600
    )
    stats = json.loads(result.stdout)

    removal = None
    try:
        execute.check(
            transport.rsync_file_argv(src, src=spec.tmp or "", dest=str(part)),
            f"pull of {spec.tmp}",
            timeout=21600,
        )
        part.replace(dest)
    finally:
        # From here the temp file is this run's own, so removing it is right
        # whether the pull worked or not: a half-pulled multi-gigabyte file
        # left on the source every night is its own outage.
        removal = execute.run(transport.remove_file_argv(src, spec.tmp or ""), timeout=60)
        part.unlink(missing_ok=True)

    # Only reached when the pull itself succeeded, so this cannot mask a more
    # important error. A cleanup that did not happen must not be reported as
    # `ok`: VACUUM INTO refuses to overwrite, so the leftover would surface as
    # an unexplained failure tomorrow night instead of a clear one now.
    if not removal.ok:
        raise JobError(
            f"the dump was pulled successfully, but {spec.tmp} could not be removed from "
            f"{src.location} (exit {removal.returncode}): {removal.stderr.strip()} — "
            "remove it by hand, or the next run will refuse to start"
        )

    return {
        "kind": "dump",
        "to": spec.dest,
        "engine": "sqlite",
        "method": spec.method,
        "path": spec.path,
        "tmp": spec.tmp,
        "bytes": stats["bytes"],
        "source_bytes": stats["source_bytes"],
        "integrity_check": stats["integrity_check"],
        "restore": manifest.restore_dump(src, spec, config.root),
    }
