"""Build the shell snippet that produces a dump on stdout.

Two properties every builder here holds to:

1. The runner never learns the secret. `config.yaml` names an *environment
   variable inside the container*, and the snippet references it as a shell
   variable. Expansion happens in the container. The value never appears in an
   argv this process builds, never crosses ssh, never enters a log.
   Scope of that claim: it does not hide the value from someone who can
   already read the container's environment — it removes this runner, its
   logs, and the source host's process table from the set of places it leaks.

2. Nothing is piped. An earlier design ran `dump | gzip` in the container and
   relied on `set -o pipefail` to notice a dump that died halfway. That is not
   portable: blog-database's /bin/sh is dash, where `set -o pipefail` is an
   illegal option, so the guard would have silently done nothing and produced
   a valid gzip of a truncated SQL file. Compression now happens on the
   receiving side where this process owns both ends of the pipe and checks
   both exit codes.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .config import DumpSpec
from .errors import ConfigError


@dataclass(frozen=True)
class DumpCommand:
    snippet: str  # runs under /bin/sh inside the container
    tool: str  # the binary, for the --version probe and the manifest


def build(spec: DumpSpec) -> DumpCommand:
    builder = {
        "mysqldump": _mysql,
        "mariadb-dump": _mysql,
        "pg_dump": _postgres,
        "mongodump": _mongo,
    }.get(spec.engine)
    if builder is None:
        raise ConfigError(f"engine {spec.engine!r} does not stream a dump")
    return builder(spec)


def version_snippet(tool: str) -> str:
    return f"exec {shlex.quote(tool)} --version"


def _env(spec: DumpSpec, key: str, default: str | None = None) -> str | None:
    """Render a reference to a container-side variable, not its value."""
    name = spec.auth.get(key)
    if name:
        return f'"${{{name}:?{name} is not set in this container}}"'
    return default


def _mysql(spec: DumpSpec) -> DumpCommand:
    tool = spec.engine
    password = _env(spec, "password_env")
    if password is None:
        raise ConfigError(f"{tool} dump {spec.dest!r} needs auth.password_env")
    user = _env(spec, "username_env", default="root")
    database = spec.database or _env(spec, "database_env")
    if not database:
        raise ConfigError(f"{tool} dump {spec.dest!r} needs a database")

    args = " ".join(shlex.quote(a) for a in spec.args)
    return DumpCommand(
        snippet=(
            f"export MYSQL_PWD={password}; "
            f"exec {tool} -u{user} {args} {shlex.quote(database)}".replace("  ", " ")
        ),
        tool=tool,
    )


def _postgres(spec: DumpSpec) -> DumpCommand:
    password = _env(spec, "password_env")
    if password is None:
        raise ConfigError(f"pg_dump dump {spec.dest!r} needs auth.password_env")
    user = _env(spec, "username_env")
    if user is None:
        raise ConfigError(f"pg_dump dump {spec.dest!r} needs auth.username_env")
    database = shlex.quote(spec.database) if spec.database else _env(spec, "database_env")
    if database is None:
        raise ConfigError(f"pg_dump dump {spec.dest!r} needs a database or auth.database_env")

    args = " ".join(shlex.quote(a) for a in spec.args)
    return DumpCommand(
        snippet=(
            f"export PGPASSWORD={password}; "
            f"exec pg_dump -U {user} -d {database} {args}".rstrip()
        ),
        tool="pg_dump",
    )


def _mongo(spec: DumpSpec) -> DumpCommand:
    user = _env(spec, "username_env")
    password = _env(spec, "password_env")
    if user is None or password is None:
        raise ConfigError(
            f"mongodump {spec.dest!r} needs auth.username_env and auth.password_env"
        )
    args = " ".join(shlex.quote(a) for a in spec.args)
    # mongodump has no password environment variable, so unlike the other two
    # engines the credential does land in an argv — inside the container only,
    # where the mongod process it authenticates to is already running.
    return DumpCommand(
        snippet=(
            f"exec mongodump -u {user} -p {password} "
            f"--authenticationDatabase admin {args}".rstrip()
        ),
        tool="mongodump",
    )
