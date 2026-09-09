"""Validation is the cheapest place to catch a backup that would look fine."""

import copy

import pytest

from runner import config as cfg
from runner.errors import ConfigError

BASE = {
    "version": 1,
    "destination": {"root": "/mnt/backup/hosts", "snapshot": {"host": "pve02", "dataset": "tank/backup/hosts"}},
    "sources": {
        "prd01": {"ssh": "marshall@10.0.0.1", "privilege": "sudo-rsync"},
        "here": {"local": True},
        "k3s": {"kubectl": {}},
    },
    "jobs": [
        {
            "name": "blog",
            "source": "prd01",
            "paths": [{"from": "/srv/blog", "to": "blog/files", "exclude": ["/db/"]}],
            "dumps": [
                {
                    "to": "blog/dump/mongo.archive.gz",
                    "engine": "mongodump",
                    "container": "blog-database",
                    "args": ["--archive", "--gzip"],
                    "auth": {"username_env": "U", "password_env": "P"},
                }
            ],
        }
    ],
}


def build(**patch):
    raw = copy.deepcopy(BASE)
    for key, value in patch.items():
        raw[key] = value
    return raw


def job(**patch):
    entry = copy.deepcopy(BASE["jobs"][0])
    entry.update(patch)
    return build(jobs=[entry])


def test_parses_the_happy_path():
    parsed = cfg.parse(BASE)
    assert [j.name for j in parsed.jobs] == ["blog"]
    assert parsed.jobs[0].bundle == "blog"
    assert parsed.sources["prd01"].mode == "ssh"


def test_the_real_config_loads():
    from pathlib import Path

    parsed = cfg.load(Path(__file__).resolve().parents[1] / "config.yaml")
    assert len(parsed.jobs) == 10
    assert {j.bundle for j in parsed.jobs} == {
        "blog", "dongjoo", "n8n", "miniflux", "infisical", "storage01", "k3s",
        "db01", "edge01", "app01",
    }
    # Every job has to name a host that still holds the data. The whole point of
    # moving `source` in the same change as the stack is that a job left behind
    # keeps succeeding against a stale copy, which is worse than failing.
    assert {j.name: j.source for j in parsed.jobs} == {
        "blog": "app01", "dongjoo": "app01", "n8n": "app01", "app01-misc": "app01",
        "miniflux-cold": "app01",
        "infisical": "k3s", "edge01": "edge01", "k3s-server": "k3s01",
        "storage01-db": "storage01", "db01": "db01",
    }
    # prd01 is gone. Its absence is the assertion — a source left behind here is a
    # job that keeps succeeding against a host nobody is watching any more.
    assert "prd01" not in parsed.sources


def test_rejects_a_future_schema_version():
    with pytest.raises(ConfigError, match="unsupported version"):
        cfg.parse(build(version=2))


def test_rejects_an_unknown_source():
    with pytest.raises(ConfigError, match="unknown source"):
        cfg.parse(job(source="nope"))


def test_rejects_an_unknown_engine():
    entry = copy.deepcopy(BASE["jobs"][0])
    entry["dumps"][0]["engine"] = "cassandra-dump"
    with pytest.raises(ConfigError, match="unknown engine"):
        cfg.parse(build(jobs=[entry]))


def test_rejects_a_job_that_collects_nothing():
    with pytest.raises(ConfigError, match="neither paths nor dumps"):
        cfg.parse(job(paths=[], dumps=[]))


def test_rejects_paths_on_a_kubectl_source():
    with pytest.raises(ConfigError, match="no filesystem"):
        cfg.parse(job(source="k3s", dumps=[]))


@pytest.mark.parametrize("dest", ["/blog/files", "../escape", "blog/../../etc", ""])
def test_rejects_a_destination_that_escapes_the_root(dest):
    with pytest.raises(ConfigError):
        cfg.parse(job(paths=[{"from": "/srv/blog", "to": dest}], dumps=[]))


def test_rejects_a_job_whose_outputs_have_no_common_home():
    """The manifest lives next to the output, which needs the output to be one place."""
    with pytest.raises(ConfigError, match="share one top-level directory"):
        cfg.parse(
            job(
                paths=[
                    {"from": "/a", "to": "one/files"},
                    {"from": "/b", "to": "two/files"},
                ],
                dumps=[],
            )
        )


def test_rejects_two_jobs_claiming_the_same_bundle():
    first = copy.deepcopy(BASE["jobs"][0])
    second = copy.deepcopy(BASE["jobs"][0])
    second["name"] = "blog-extra"
    with pytest.raises(ConfigError, match="manifests would collide"):
        cfg.parse(build(jobs=[first, second]))


def test_rejects_an_unknown_precondition():
    with pytest.raises(ConfigError, match="unknown precondition"):
        cfg.parse(job(precondition={"disk_is_cold": "yes"}))


def test_rejects_a_self_compressing_dump_written_without_gz():
    entry = copy.deepcopy(BASE["jobs"][0])
    entry["dumps"][0]["to"] = "blog/dump/mongo.archive"
    with pytest.raises(ConfigError, match="does not end in .gz"):
        cfg.parse(build(jobs=[entry]))


def test_mongodump_with_gzip_is_not_compressed_twice():
    spec = cfg.parse(BASE).jobs[0].dumps[0]
    assert spec.self_compressed is True
    assert spec.compress is False


def test_a_plain_sql_dump_written_as_gz_is_compressed_by_the_runner():
    entry = copy.deepcopy(BASE["jobs"][0])
    entry["dumps"] = [
        {
            "to": "blog/dump/x.sql.gz",
            "engine": "mysqldump",
            "container": "c",
            "database": "d",
            "auth": {"password_env": "P"},
        }
    ]
    spec = cfg.parse(build(jobs=[entry])).jobs[0].dumps[0]
    assert spec.self_compressed is False
    assert spec.compress is True


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"path": "relative.sqlite"}, "absolute `path`"),
        ({"method": "dot-backup"}, "vacuum-into"),
        ({"tmp": None}, "absolute `tmp`"),
        ({"to": "n8n/dump/db.sqlite.gz"}, "not a compressed stream"),
    ],
)
def test_sqlite_dump_requirements(patch, message):
    dump = {
        "to": "n8n/dump/database.sqlite",
        "engine": "sqlite",
        "path": "/data/database.sqlite",
        "method": "vacuum-into",
        "tmp": "/tmp/db.sqlite",
    }
    dump.update(patch)
    with pytest.raises(ConfigError, match=message):
        cfg.parse(job(paths=[], dumps=[dump]))


def test_an_ssh_source_may_omit_privilege_when_it_only_dumps():
    raw = build()
    raw["sources"]["dumper"] = {"ssh": "marshall@db01"}
    raw["jobs"] = [
        {
            "name": "d",
            "source": "dumper",
            "dumps": [
                {
                    "to": "d/dump/all.sql.gz",
                    "engine": "pg_dumpall",
                    "container": "postgres",
                    "auth": {"username_env": "U", "password_env": "P"},
                }
            ],
        }
    ]
    parsed = cfg.parse(raw)
    assert parsed.sources["dumper"].privilege is None


def test_a_privilegeless_ssh_source_cannot_pull_paths():
    raw = build()
    raw["sources"]["dumper"] = {"ssh": "marshall@db01"}
    raw["jobs"] = [
        {"name": "d", "source": "dumper", "paths": [{"from": "/srv", "to": "d/files"}]}
    ]
    with pytest.raises(ConfigError, match="only.*run dumps"):
        cfg.parse(raw)


def test_rejects_an_unknown_privilege_value():
    raw = build()
    raw["sources"]["dumper"] = {"ssh": "marshall@db01", "privilege": "root"}
    with pytest.raises(ConfigError, match="sudo-rsync' or omitted"):
        cfg.parse(raw)
