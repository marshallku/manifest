"""Ask the hypervisor to freeze what was just collected.

The runner does not run `zfs` — the pool is on pve02, not in this VM. It asks
over ssh with a key whose forced command can only *create* a snapshot of one
dataset. Destroying and replicating are pve02's own business, so no bug here
and no compromise of this VM can erase backup history.

The snapshot is caused by a completed collection rather than by an independent
timer, because a timer that fires mid-rsync preserves a torn backup — worst
exactly for the jobs where a dump and its files have to agree.

Creation is then *confirmed* rather than assumed. The request is one ssh call
whose only evidence of success is an exit code, and an exit code is produced by
the forced command on pve02 — not by the pool. A forced command that was
replaced, renamed or turned into a no-op would keep exiting 0 while nothing is
ever frozen, and every run would look healthy. Since `destination.root` is the
dataset's own mountpoint, `.zfs/snapshot/<name>` answers the question locally
and without any additional privilege.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .errors import JobError
from .source import SSH_OPTS

DEFAULT_USER = "backupsnap"
NAME = re.compile(r"^backup-\d{8}T\d{6}Z$")


def name_for(when: datetime | None = None) -> str:
    return (when or datetime.now(UTC)).strftime("backup-%Y%m%dT%H%M%SZ")


def argv_for(config: Config, name: str) -> list[str]:
    if not NAME.match(name):
        raise JobError(f"refusing to request snapshot {name!r}: not of the form backup-<stamp>")
    target = config.snapshot_host
    if "@" not in target:
        target = f"{DEFAULT_USER}@{target}"
    # The name is the entire remote command; the forced command on pve02 reads
    # it from SSH_ORIGINAL_COMMAND, validates it again, and pins the dataset.
    return ["ssh", *SSH_OPTS, target, name]


def take(config: Config, *, log, dry_run: bool = False) -> dict:
    from . import execute

    name = name_for()
    argv = argv_for(config, name)
    log(f"snapshot: {config.snapshot_host}:{config.snapshot_dataset}@{name}")
    if dry_run:
        log(f"  $ {shlex.join(argv)}")
        return {"name": name, "dry_run": True}

    result = execute.run(argv, timeout=120)
    if not result.ok:
        raise JobError(f"snapshot request failed (exit {result.returncode}): {result.stderr.strip()}")
    confirm(config.root, name)
    return {
        "name": name,
        "dataset": config.snapshot_dataset,
        "host": config.snapshot_host,
        "confirmed_at": str(snapshot_dir(config.root, name)),
    }


def snapshot_dir(root: Path, name: str) -> Path:
    return root / ".zfs" / "snapshot" / name


def confirm(root: Path, name: str) -> None:
    """Fail unless the snapshot the hypervisor reported creating is really there.

    Fails closed on an unreadable or absent `.zfs`, rather than downgrading to
    "could not check". A run that cannot see the snapshot directory has no
    evidence its history is being kept, and reporting that as success is the
    exact failure this function exists to catch.
    """
    if not snapshot_dir(root, name).is_dir():
        raise JobError(
            f"pve02 reported creating {name} but {snapshot_dir(root, name)} does not exist. "
            f"Either the forced command is not snapshotting {root}'s dataset, or this host "
            f"cannot see `.zfs` (the destination must be the dataset's own mountpoint)."
        )
