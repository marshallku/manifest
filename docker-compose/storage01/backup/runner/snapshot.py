"""Ask the hypervisor to freeze what was just collected.

The runner does not run `zfs` — the pool is on pve02, not in this VM. It asks
over ssh with a key whose forced command can only *create* a snapshot of one
dataset. Destroying and replicating are pve02's own business, so no bug here
and no compromise of this VM can erase backup history.

The snapshot is caused by a completed collection rather than by an independent
timer, because a timer that fires mid-rsync preserves a torn backup — worst
exactly for the jobs where a dump and its files have to agree.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime

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
    return {"name": name, "dataset": config.snapshot_dataset, "host": config.snapshot_host}
