"""What was collected, and the command that puts it back.

A snapshot on its own is a pile of files. Restoring a service needs the dump
*and* its files *and* the ownership *and* the knowledge that some directory was
deliberately excluded. Six months from now that context exists only if it was
written down at collection time — so the manifest is unconditional and has no
config flag, and it is written for failed jobs too.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from .config import Config, DumpSpec, Job, PathSpec, Source

SCHEMA = 1


def restore_paths(source: Source, spec: PathSpec, dest_root: Path) -> str:
    src = f"{dest_root}/{spec.dest}/"
    if source.mode == "local":
        return shlex.join(["rsync", "--archive", "--hard-links", "--acls", "--xattrs", "--numeric-ids", src, f"{spec.src}/"])
    # --fake-super on the SENDING side turns the stored xattrs back into real
    # ownership. `--rsync-path=sudo rsync` is required to write as root and is
    # NOT covered by the read-only sudoers rule on the source: restoring is a
    # deliberate, separately-authorised act.
    return shlex.join(
        [
            "rsync", "--archive", "--hard-links", "--acls", "--xattrs", "--numeric-ids",
            "--fake-super", "--rsync-path=sudo rsync", src, f"{source.ssh}:{spec.src}/",
        ]
    )


def restore_dump(source: Source, spec: DumpSpec, dest_root: Path) -> str:
    path = f"{dest_root}/{spec.dest}"
    target = _target(source, spec)

    if spec.engine in ("mysqldump", "mariadb-dump"):
        client = "mariadb" if spec.engine == "mariadb-dump" else "mysql"
        user = _var(spec, "username_env", "root")
        inner = f'export MYSQL_PWD={_var(spec, "password_env")}; exec {client} -u{user} {spec.database}'
        return f"gunzip -c {path} | {target} sh -c {shlex.quote(inner)}"

    if spec.engine == "pg_dump":
        database = spec.database or _var(spec, "database_env")
        inner = (
            f'export PGPASSWORD={_var(spec, "password_env")}; '
            f'exec psql -U {_var(spec, "username_env")} -d {database}'
        )
        return f"gunzip -c {path} | {target} sh -c {shlex.quote(inner)}"

    if spec.engine == "mongodump":
        inner = (
            f'exec mongorestore --archive --gzip --drop '
            f'-u {_var(spec, "username_env")} -p {_var(spec, "password_env")} '
            f"--authenticationDatabase admin"
        )
        return f"{target} sh -c {shlex.quote(inner)} < {path}"

    if spec.engine == "sqlite" and not spec.helper:
        return (
            f"# stop the service first, then: "
            f"rsync --archive {path} {source.ssh}:{spec.path} && "
            f"ssh {source.ssh} rm -f {spec.path}-wal {spec.path}-shm"
        )

    if spec.engine == "sqlite":
        # A helper-backed dump is one whose database could not be read without
        # privilege, which in practice means its directory is excluded from the
        # paths half of the job and may not exist at all on a rebuilt host.
        # "rsync the file back" is wrong here, and wrong in the way that only
        # surfaces when someone is already having a bad day, so the whole
        # procedure is written out instead of implied.
        directory = (spec.path or "").rsplit("/", 1)[0]
        pairing = (
            f"#  3. sha256sum the restored {spec.paired_file} and compare it with the "
            f"`paired_sha256` recorded beside this artifact. If they differ, STOP: the two "
            f"halves were taken at different moments and cannot be restored together.\n"
            if spec.paired_file
            else ""
        )
        staged = f"/tmp/{spec.dest.rsplit('/', 1)[-1]}"
        return (
            f"# NOT YET DRILLED. Restoring this is a procedure, not a copy, and every\n"
            f"# step runs AS ROOT ON THE TARGET HOST. The backup account cannot do it:\n"
            f"# {source.ssh} grants it `rsync --server --sender` — read access and\n"
            f"# nothing else, deliberately. A restore is a console, not this runner.\n"
            f"#  1. install the service on the target host, then STOP it.\n"
            f"#  2. restore the paths half of this job first, preserving ownership and modes.\n"
            f"{pairing}"
            f"#  4. stage the database and install it as root. Its directory is excluded\n"
            f"#     from the paths half, so on a rebuilt host it may not exist at all:\n"
            f"#       scp {path} {source.ssh}:{staged}      # any user; /tmp is writable\n"
            f"#       # then, as root on the target:\n"
            f"#       install -d -o root -g root -m 0700 {directory}\n"
            f"#       install -o root -g root -m 0644 {staged} {spec.path}\n"
            f"#       rm -f {spec.path}-wal {spec.path}-shm {staged}\n"
            f"#  5. start the service and confirm it came back with the state you expect,\n"
            f"#     not merely that the process is running."
        )

    raise ValueError(f"no restore command for engine {spec.engine!r}")


def _target(source: Source, spec: DumpSpec) -> str:
    if source.mode == "kubectl":
        namespace, pod = (spec.pod or "/").split("/", 1)
        container = f" -c {spec.container}" if spec.container else ""
        return f"kubectl exec -i -n {namespace} {pod}{container} --"
    prefix = f"ssh {source.ssh} " if source.mode == "ssh" else ""
    return f"{prefix}docker exec -i {spec.container}"


def _var(spec: DumpSpec, key: str, default: str | None = None) -> str:
    name = spec.auth.get(key)
    if name:
        return f'"${name}"'
    if default is None:
        raise ValueError(f"dump {spec.dest!r} has no auth.{key}")
    return default


def for_job(
    config: Config,
    job: Job,
    *,
    status: str,
    started_at: str,
    finished_at: str,
    artifacts: list[dict],
    error: str | None,
) -> dict:
    source = config.sources[job.source]
    return {
        "schema": SCHEMA,
        "job": job.name,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "error": error,
        "source": {"name": source.name, "mode": source.mode, "location": source.location},
        "destination": {"root": str(config.root), "bundle": job.bundle},
        "precondition": job.precondition or None,
        "runner": {"host": os.uname().nodename, "version": _runner_version()},
        "artifacts": artifacts,
        "snapshots": (
            f"{config.snapshot_host}:{config.snapshot_dataset}  "
            "(browse under <mountpoint>/.zfs/snapshot/<stamp>/)"
        ),
    }


def _runner_version() -> str:
    from . import VERSION

    return VERSION


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.parent / (path.name + ".part")
    part.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    part.replace(path)
