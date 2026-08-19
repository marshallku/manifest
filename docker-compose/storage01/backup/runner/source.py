"""How to reach a source host, and how to run something there.

Every other module is written against these two primitives — a command to
execute and a way to pull files — so a new source mode is a change to this
file alone.
"""

from __future__ import annotations

import shlex

from .config import Source
from .errors import JobError

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
]


def wrap(source: Source, argv: list[str]) -> list[str]:
    """Turn a command meant for the source host into one to run locally."""
    if source.mode == "local":
        return list(argv)
    if source.mode == "ssh":
        # `ssh host -- cmd` does NOT work: after the destination, ssh treats
        # every remaining token as part of the remote command, so the `--`
        # itself would be executed. Join into one string instead and let the
        # remote shell take it.
        return ["ssh", *SSH_OPTS, source.ssh, shlex.join(argv)]
    raise JobError(f"source {source.name!r} ({source.mode}) cannot run host commands")


def exec_in_container(source: Source, container: str, snippet: str) -> list[str]:
    """Run a /bin/sh snippet inside a container on this source.

    The snippet is deliberately the unit of execution rather than an argv:
    credentials are referenced as shell variables that only exist inside the
    container, so they are expanded there and never enter any argv this
    runner builds. See dumps.py.
    """
    return wrap(source, ["docker", "exec", container, "sh", "-c", snippet])


def exec_in_pod(source: Source, pod: str, container: str | None, snippet: str) -> list[str]:
    if source.mode != "kubectl":
        raise JobError(f"source {source.name!r} is not a kubectl source")
    if "/" not in pod:
        raise JobError(f"pod {pod!r} must be given as <namespace>/<name>")
    namespace, name = pod.split("/", 1)
    argv = ["kubectl", "exec", "-n", namespace, name]
    if container:
        argv += ["-c", container]
    # Unlike ssh, kubectl really does use `--` as the argument terminator.
    return [*argv, "--", "sh", "-c", snippet]


def python_program(source: Source, args: list[str]) -> list[str]:
    """Run a program supplied on stdin with the source's own python3."""
    return wrap(source, ["python3", "-", *args])


def rsync_argv(
    source: Source,
    *,
    src: str,
    dest: str,
    exclude: tuple[str, ...] = (),
    dry_run: bool = False,
) -> list[str]:
    """Pull `src` from the source into the local absolute path `dest`.

    -aHAX --numeric-ids keeps hardlinks, ACLs and xattrs; --delete makes the
    destination a mirror rather than an ever-growing union (history is the
    snapshot's job, not the working copy's).
    """
    argv = [
        "rsync",
        "--archive",
        "--hard-links",
        "--acls",
        "--xattrs",
        "--numeric-ids",
        "--delete",
        "--delete-excluded",
        "--info=stats2",
    ]
    if dry_run:
        argv.append("--dry-run")
    for pattern in exclude:
        argv.append(f"--exclude={pattern}")

    if source.mode == "local":
        remote_src = f"{src}/"
    elif source.mode == "ssh":
        # --fake-super stores the source's uid/gid/mode in an xattr instead of
        # applying them, so the receiving rsync needs no privilege at all.
        argv += ["--fake-super", "--rsync-path=sudo rsync", "-e", " ".join(["ssh", *SSH_OPTS])]
        remote_src = f"{source.ssh}:{shlex.quote(src)}/"
    else:
        raise JobError(f"source {source.name!r} ({source.mode}) has no filesystem to pull from")

    return [*argv, remote_src, f"{dest}/"]


def container_state_argv(source: Source, container: str) -> list[str]:
    """Ask for a container's state in a way that distinguishes absent from broken.

    `docker inspect` exits nonzero for a missing container — but also for a
    dead daemon, a denied socket, or an ssh that never connected. Reading all
    of those as "absent" would satisfy a `container_not_running` precondition
    by accident, which is the one outcome that precondition exists to prevent.
    `docker ps` exits 0 either way and prints nothing when there is no match,
    so absence is data and failure stays failure.
    """
    return wrap(
        source,
        ["docker", "ps", "--all", "--no-trunc", f"--filter=name=^{container}$", "--format={{.State}}"],
    )


def rsync_file_argv(source: Source, *, src: str, dest: str) -> list[str]:
    """Pull a single file. Used for the SQLite copy, which cannot stream."""
    argv = ["rsync", "--archive", "--numeric-ids", "--info=stats2"]
    if source.mode == "local":
        return [*argv, src, dest]
    if source.mode == "ssh":
        argv += ["--fake-super", "--rsync-path=sudo rsync", "-e", " ".join(["ssh", *SSH_OPTS])]
        return [*argv, f"{source.ssh}:{shlex.quote(src)}", dest]
    raise JobError(f"source {source.name!r} ({source.mode}) has no filesystem to pull from")


def remove_file_argv(source: Source, path: str) -> list[str]:
    return wrap(source, ["rm", "-f", path])
