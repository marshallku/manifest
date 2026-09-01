"""Load and validate config.yaml.

Every check here exists because the alternative is a backup that looks fine
and is not. Validation is total: the runner refuses to start on any violation
rather than skipping the offending job, because a skipped job is invisible.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

SUPPORTED_VERSION = 1

# Engines that stream their dump to stdout, and the CLI they need present in
# the container. `sqlite` is absent on purpose: it cannot stream (see dumps.py).
STREAMING_ENGINES = {
    "mysqldump": "mysqldump",
    "mariadb-dump": "mariadb-dump",
    "pg_dump": "pg_dump",
    "mongodump": "mongodump",
}
ENGINES = set(STREAMING_ENGINES) | {"sqlite"}

PRECONDITIONS = {"container_not_running"}


@dataclass(frozen=True)
class Source:
    name: str
    mode: str  # ssh | local | kubectl
    ssh: str | None = None
    privilege: str | None = None

    @property
    def location(self) -> str:
        return {"ssh": self.ssh or "", "local": "localhost", "kubectl": "k3s"}[self.mode]


@dataclass(frozen=True)
class PathSpec:
    src: str
    dest: str
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class DumpSpec:
    dest: str
    engine: str
    container: str | None = None
    pod: str | None = None
    database: str | None = None
    args: tuple[str, ...] = ()
    auth: dict[str, str] = field(default_factory=dict)
    # sqlite only
    path: str | None = None
    method: str | None = None
    tmp: str | None = None
    # sqlite only, and only when the database is unreadable without privilege:
    # the absolute path of a root helper on the source that produces the copy
    # and reports it as JSON. See runner/sqlite_dump.py for the contract.
    helper: str | None = None
    # sqlite+helper only. A destination-relative artifact whose sha256 must
    # equal the `paired_sha256` the helper reports. It is how a dump and the
    # separately-rsynced file it must be restored alongside are proven to
    # belong together.
    paired_file: str | None = None

    @property
    def self_compressed(self) -> bool:
        """True when the engine emits an already-compressed stream.

        Piping such a stream through gzip again produces a double-compressed
        artifact and a restore command that is wrong in a way nobody notices
        until the restore.
        """
        return self.engine == "mongodump" and "--gzip" in self.args

    @property
    def compress(self) -> bool:
        return self.dest.endswith(".gz") and not self.self_compressed


@dataclass(frozen=True)
class Job:
    name: str
    source: str
    bundle: str
    paths: tuple[PathSpec, ...] = ()
    dumps: tuple[DumpSpec, ...] = ()
    precondition: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    root: Path
    snapshot_host: str
    snapshot_dataset: str
    sources: dict[str, Source]
    jobs: tuple[Job, ...]


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return parse(raw)


def parse(raw: dict[str, Any]) -> Config:
    version = raw.get("version")
    if version != SUPPORTED_VERSION:
        raise ConfigError(f"unsupported version {version!r}, this runner speaks {SUPPORTED_VERSION}")

    dest = _require_mapping(raw, "destination")
    root = dest.get("root")
    if not isinstance(root, str) or not root.startswith("/"):
        raise ConfigError("destination.root must be an absolute path")
    snap = _require_mapping(dest, "snapshot")
    for key in ("host", "dataset"):
        if not isinstance(snap.get(key), str) or not snap[key]:
            raise ConfigError(f"destination.snapshot.{key} must be a non-empty string")

    sources = _parse_sources(_require_mapping(raw, "sources"))
    jobs = _parse_jobs(raw.get("jobs"), sources)

    return Config(
        root=Path(root),
        snapshot_host=snap["host"],
        snapshot_dataset=snap["dataset"],
        sources=sources,
        jobs=jobs,
    )


def _require_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _parse_sources(raw: dict[str, Any]) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"sources.{name} must be a mapping")
        modes = [m for m in ("ssh", "local", "kubectl") if m in spec]
        if len(modes) != 1:
            raise ConfigError(
                f"sources.{name} must declare exactly one of ssh/local/kubectl, found {modes or 'none'}"
            )
        mode = modes[0]
        if mode == "ssh":
            if spec.get("privilege") != "sudo-rsync":
                raise ConfigError(f"sources.{name}.privilege must be 'sudo-rsync'")
            sources[name] = Source(name, "ssh", ssh=spec["ssh"], privilege="sudo-rsync")
        elif mode == "local":
            if spec["local"] is not True:
                raise ConfigError(f"sources.{name}.local must be true")
            sources[name] = Source(name, "local")
        else:
            sources[name] = Source(name, "kubectl")
    if not sources:
        raise ConfigError("sources is empty")
    return sources


def _parse_jobs(raw: Any, sources: dict[str, Source]) -> tuple[Job, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("jobs must be a non-empty list")

    jobs: list[Job] = []
    seen_names: set[str] = set()
    # Two jobs writing into the same top-level directory would overwrite each
    # other's manifest.json, so bundles are claimed exclusively.
    bundle_owner: dict[str, str] = {}

    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError("each job must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError("job.name must be a non-empty string")
        if name in seen_names:
            raise ConfigError(f"duplicate job name {name!r}")
        seen_names.add(name)

        source_name = entry.get("source")
        if source_name not in sources:
            raise ConfigError(f"job {name!r}: unknown source {source_name!r}")
        source = sources[source_name]

        paths = tuple(_parse_path(name, p) for p in entry.get("paths") or [])
        dumps = tuple(_parse_dump(name, d) for d in entry.get("dumps") or [])
        if not paths and not dumps:
            raise ConfigError(f"job {name!r} declares neither paths nor dumps")
        if paths and source.mode == "kubectl":
            raise ConfigError(f"job {name!r}: a kubectl source has no filesystem to pull from")

        precondition = entry.get("precondition") or {}
        if not isinstance(precondition, dict):
            raise ConfigError(f"job {name!r}: precondition must be a mapping")
        unknown = set(precondition) - PRECONDITIONS
        if unknown:
            raise ConfigError(f"job {name!r}: unknown precondition(s) {sorted(unknown)}")

        bundle = _bundle_of(name, [p.dest for p in paths] + [d.dest for d in dumps])
        if bundle in bundle_owner:
            raise ConfigError(
                f"job {name!r} writes into {bundle!r}, already claimed by job "
                f"{bundle_owner[bundle]!r} — their manifests would collide"
            )
        bundle_owner[bundle] = name

        jobs.append(Job(name, source_name, bundle, paths, dumps, precondition))

    return tuple(jobs)


def _parse_path(job: str, raw: Any) -> PathSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"job {job!r}: each entry of paths must be a mapping")
    src = raw.get("from")
    if not isinstance(src, str) or not src.startswith("/"):
        raise ConfigError(f"job {job!r}: paths[].from must be an absolute path")
    dest = _check_dest(job, raw.get("to"))
    exclude = raw.get("exclude") or []
    if not isinstance(exclude, list) or not all(isinstance(e, str) for e in exclude):
        raise ConfigError(f"job {job!r}: paths[].exclude must be a list of strings")
    return PathSpec(src=src.rstrip("/"), dest=dest, exclude=tuple(exclude))


def _parse_dump(job: str, raw: Any) -> DumpSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"job {job!r}: each entry of dumps must be a mapping")
    dest = _check_dest(job, raw.get("to"))
    engine = raw.get("engine")
    if engine not in ENGINES:
        raise ConfigError(f"job {job!r}: unknown engine {engine!r}, known: {sorted(ENGINES)}")

    args = raw.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConfigError(f"job {job!r}: dumps[].args must be a list of strings")
    auth = raw.get("auth") or {}
    if not isinstance(auth, dict) or not all(isinstance(v, str) for v in auth.values()):
        raise ConfigError(f"job {job!r}: dumps[].auth must be a mapping of strings")
    unknown_auth = set(auth) - {"username_env", "password_env", "database_env"}
    if unknown_auth:
        raise ConfigError(f"job {job!r}: unknown auth key(s) {sorted(unknown_auth)}")

    spec = DumpSpec(
        dest=dest,
        engine=engine,
        container=raw.get("container"),
        pod=raw.get("pod"),
        database=raw.get("database"),
        args=tuple(args),
        auth=auth,
        path=raw.get("path"),
        method=raw.get("method"),
        tmp=raw.get("tmp"),
        helper=raw.get("helper"),
        paired_file=raw.get("paired_file"),
    )

    if engine == "sqlite":
        if not spec.path or not spec.path.startswith("/"):
            raise ConfigError(f"job {job!r}: a sqlite dump needs an absolute `path`")
        if spec.method != "vacuum-into":
            raise ConfigError(f"job {job!r}: the only supported sqlite method is 'vacuum-into'")
        if not spec.tmp or not spec.tmp.startswith("/"):
            raise ConfigError(f"job {job!r}: a sqlite dump needs an absolute `tmp` on the source")
        if dest.endswith(".gz"):
            raise ConfigError(f"job {job!r}: a sqlite dump is a database file, not a compressed stream")
        if spec.helper is not None and not spec.helper.startswith("/"):
            raise ConfigError(f"job {job!r}: `helper` must be an absolute path on the source")
        if spec.paired_file is not None:
            if spec.helper is None:
                raise ConfigError(f"job {job!r}: `paired_file` only means something with a `helper`")
            # Checked with the same rule as `to:`, because it is resolved
            # against destination.root exactly like one.
            _check_dest(job, spec.paired_file)
    else:
        if not spec.container and not spec.pod:
            raise ConfigError(f"job {job!r}: dump {dest!r} needs a container (or pod, for kubectl)")
        if spec.self_compressed and not dest.endswith(".gz"):
            raise ConfigError(
                f"job {job!r}: dump {dest!r} compresses its own output but the destination "
                "does not end in .gz"
            )

    return spec


def _check_dest(job: str, dest: Any) -> str:
    """A `to:` is joined onto destination.root, so it must not escape it."""
    if not isinstance(dest, str) or not dest:
        raise ConfigError(f"job {job!r}: `to` must be a non-empty string")
    if dest.startswith("/"):
        raise ConfigError(f"job {job!r}: `to` is relative to destination.root, drop the leading /")
    normalised = posixpath.normpath(dest)
    if normalised != dest.rstrip("/") or normalised.startswith(".."):
        raise ConfigError(f"job {job!r}: `to` must be a plain relative path, got {dest!r}")
    return normalised


def _bundle_of(job: str, dests: list[str]) -> str:
    """The single top-level directory a job owns.

    The manifest lives next to the job's output, which only has a meaning if
    all of that output shares one root directory.
    """
    segments = {d.split("/")[0] for d in dests}
    if len(segments) != 1:
        raise ConfigError(
            f"job {job!r}: every `to` must share one top-level directory so the manifest has a "
            f"home, got {sorted(segments)}"
        )
    bundle = segments.pop()
    if not bundle or bundle in (".", ".."):
        raise ConfigError(f"job {job!r}: invalid top-level directory {bundle!r}")
    return bundle
