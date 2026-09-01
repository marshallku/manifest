"""Run one job: check its assumptions, pull its files, take its dumps.

Jobs are isolated from each other — one failure does not stop the rest — but
it does stop the snapshot, so a partial collection is never preserved as if it
were whole.
"""

from __future__ import annotations

import hashlib
import json
import re
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


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sqlite_stats(spec: DumpSpec, stdout: bytes) -> dict:
    """Validate the one JSON object a sqlite copy reports about itself.

    The inline program is this repo's own text and could be trusted loosely.
    A helper cannot: it is a file on another host, installed by hand, and the
    thing it guards is the only copy of the cluster's identity. So the contract
    is checked rather than assumed, and every way of failing it is a JobError
    rather than a KeyError or a TypeError three frames later.
    """
    what = spec.helper or "the vacuum-into program"

    def no_duplicates(pairs):
        # json.loads keeps the last of a repeated key and says nothing. Here
        # that would mean two values for `sha256` and no way to know which one
        # described the bytes, so it is a broken contract rather than a quirk.
        seen = [key for key, _ in pairs]
        repeated = sorted({key for key in seen if seen.count(key) > 1})
        if repeated:
            raise JobError(f"{what} printed duplicate key(s) {repeated} in its JSON")
        return dict(pairs)

    try:
        stats = json.loads(stdout, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as exc:
        raise JobError(f"{what} did not print one JSON object: {exc}; got {stdout[:200]!r}") from exc
    if not isinstance(stats, dict):
        raise JobError(f"{what} printed {type(stats).__name__}, not a JSON object")

    # An exact key set, not a minimum. A field this code does not know about is
    # a contract that moved without this code moving with it.
    expected = {"source_bytes", "bytes", "integrity_check"}
    if spec.helper:
        expected |= {"artifact", "sha256", "paired_sha256"}
    if set(stats) != expected:
        raise JobError(
            f"{what} printed keys {sorted(stats)}, expected exactly {sorted(expected)}"
        )

    for key in ("source_bytes", "bytes"):
        value = stats.get(key)
        # bool is an int in Python and would pass a naive isinstance check.
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise JobError(f"{what} reported {key}={value!r}, which is not a size")
    if stats["bytes"] == 0:
        raise JobError(f"{what} reported a zero-byte copy of {spec.path}")
    if stats.get("integrity_check") != "ok":
        raise JobError(f"integrity_check on the copy of {spec.path}: {stats.get('integrity_check')!r}")

    if spec.helper:
        artifact = stats.get("artifact")
        if not isinstance(artifact, str) or not artifact.startswith("/"):
            raise JobError(f"{what} reported artifact={artifact!r}, which is not an absolute path")
        # One authority on where the copy is. Without this the helper could
        # write one path while config.yaml makes the runner pull another, and
        # the run would report success over a file nobody produced today.
        if artifact != spec.tmp:
            raise JobError(
                f"{what} wrote {artifact}, but config.yaml says the copy is at {spec.tmp}. "
                "Reconcile the two before trusting this backup."
            )
        digest = stats.get("sha256")
        if not isinstance(digest, str) or not SHA256.match(digest):
            raise JobError(f"{what} reported sha256={digest!r}, which is not a sha256")
    if spec.paired_file:
        digest = stats.get("paired_sha256")
        if not isinstance(digest, str) or not SHA256.match(digest):
            raise JobError(f"{what} reported paired_sha256={digest!r}, which is not a sha256")
    return stats


def _confirm_artifact(spec: DumpSpec, stats: dict, pulled: Path) -> None:
    """Bind the bytes that arrived to the report that described them.

    The helper writes to a fixed path and the sudo grant that runs it is not
    exclusive, so between the report and the pull a second invocation could
    replace the artifact. Everything already checked — integrity, the token
    pairing — would then describe a file that is no longer the one being
    stored. Comparing digests is what makes that impossible rather than
    unlikely, and it costs one pass over a file already on local disk.
    """
    if not spec.helper:
        return
    digest = hashlib.sha256(pulled.read_bytes()).hexdigest()
    if digest != stats["sha256"]:
        raise JobError(
            f"{spec.tmp} changed between {spec.helper} reporting it and this run pulling it "
            f"(reported {stats['sha256']}, pulled {digest}). Nothing about the copy that was "
            "checked — integrity, the token it pairs with — describes the file that arrived."
        )


def _confirm_pairing(config: Config, spec: DumpSpec, stats: dict) -> str | None:
    """Prove the dump and the file it must be restored beside belong together.

    The helper hashes its companion before and after the copy, which closes the
    window around the copy itself. It cannot close the wider one: `paths` are
    rsynced before `dumps` run, so the companion this run *stored* could still
    be older than the one the helper *saw*. Comparing the stored file's hash to
    the helper's is what makes that impossible to miss, on every run rather
    than only when someone remembers to look.
    """
    if not spec.paired_file:
        return None
    stored = config.root / spec.paired_file
    if not stored.is_file():
        raise JobError(
            f"{spec.dest} declares paired_file {spec.paired_file}, but no such file was stored. "
            "The paths entry that should have brought it is missing or excluded it."
        )
    digest = hashlib.sha256(stored.read_bytes()).hexdigest()
    if digest != stats["paired_sha256"]:
        raise JobError(
            f"{spec.paired_file} changed between being stored and the dump being taken "
            f"(stored {digest}, dump was validated against {stats['paired_sha256']}). "
            "The two halves cannot be restored together; re-run once the source has settled."
        )
    return digest


def _take_sqlite_dump(config: Config, job: Job, spec: DumpSpec, *, log: Log, dry_run: bool) -> dict:
    src = config.sources[job.source]
    if spec.helper:
        # The database is unreadable without privilege, so the copy is made by
        # a root script that sudoers pins by path. It takes no arguments and
        # reads no input for exactly that reason: there is nothing to smuggle
        # through, so the grant authorises one behaviour rather than a program.
        argv = transport.wrap(src, ["sudo", "-n", spec.helper])
        program = None
        how = f"via the pinned root helper {spec.helper}"
    else:
        argv = transport.python_program(src, [spec.path or "", spec.tmp or ""])
        program = sqlite_dump.PROGRAM.encode()
        how = "VACUUM INTO via the source's python3"

    log(f"    dump: sqlite {spec.path} -> {spec.dest}  ({how})")
    if dry_run:
        log(f"      $ {shlex.join(argv)}" + ("" if spec.helper else " < <vacuum-into program>"))
        return {"kind": "dump", "to": spec.dest, "engine": "sqlite", "dry_run": True}

    dest = config.root / spec.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / (dest.name + execute.PART_SUFFIX)

    # Deliberately OUTSIDE the cleanup below. If this fails the temp file is
    # not ours — most importantly when it failed *because* a leftover from a
    # killed run is sitting there. Deleting it here would destroy the evidence
    # and turn a condition that demands a look into one that merely fails once.
    result = execute.check(argv, f"VACUUM INTO on {spec.path}", input=program, timeout=3600)
    stats = _sqlite_stats(spec, result.stdout)

    removal = None
    try:
        execute.check(
            transport.rsync_file_argv(src, src=spec.tmp or "", dest=str(part)),
            f"pull of {spec.tmp}",
            timeout=21600,
        )
        _confirm_artifact(spec, stats, part)
        part.replace(dest)
    finally:
        # From here the temp file is this run's own, so removing it is right
        # whether the pull worked or not: a half-pulled multi-gigabyte file
        # left on the source every night is its own outage.
        #
        # A helper's artifact is the exception, and is deliberately left alone:
        # it lives in a root-only directory precisely so that no unprivileged
        # process can touch it, which includes this one. The helper removes its
        # own previous artifact before writing the next, so nothing accumulates
        # and nothing goes stale.
        removal = None if spec.helper else execute.run(
            transport.remove_file_argv(src, spec.tmp or ""), timeout=60
        )
        part.unlink(missing_ok=True)

    # Only reached when the pull itself succeeded, so this cannot mask a more
    # important error. A cleanup that did not happen must not be reported as
    # `ok`: VACUUM INTO refuses to overwrite, so the leftover would surface as
    # an unexplained failure tomorrow night instead of a clear one now.
    if removal is not None and not removal.ok:
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
        # Recorded only where it already exists. A helper reports the digest of
        # what it wrote, so keeping it costs nothing and gives the `vault`
        # replica and the offsite copy something to be checked against later.
        # The piped-program path has no source-side digest, and hashing n8n's
        # 5.8 GB nightly to invent one is a cost nobody has asked for yet.
        "sha256": stats.get("sha256"),
        "paired_file": spec.paired_file,
        "paired_sha256": _confirm_pairing(config, spec, stats),
        "restore": manifest.restore_dump(src, spec, config.root),
    }
