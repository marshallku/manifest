"""A sqlite copy made by a pinned root helper, and the checks that guard it.

The inline vacuum-into program is this repo's own text. A helper is not: it is
a file on another host, installed by hand, running as root, and the thing it
protects is the only copy of the cluster's identity. Every assumption about
what it prints is therefore a test here rather than a comment there.
"""

import hashlib
import json

import pytest

from runner import config as cfg, execute, jobs, sqlite_dump
from runner.errors import ConfigError, JobError

HELPER = "/usr/local/sbin/k3s-datastore-snapshot"
TMP = "/var/lib/k3s-datastore-backup/state.db"

RAW = {
    "version": 1,
    "destination": {"root": "/replaced", "snapshot": {"host": "pve02", "dataset": "tank/backup/hosts"}},
    "sources": {"prd01": {"ssh": "marshall@10.0.0.1", "privilege": "sudo-rsync"}},
    "jobs": [
        {
            "name": "k3s-server",
            "source": "prd01",
            "paths": [{"from": "/var/lib/rancher/k3s/server", "to": "k3s/server"}],
            "dumps": [
                {
                    "to": "k3s/dump/state.db",
                    "engine": "sqlite",
                    "path": "/var/lib/rancher/k3s/server/db/state.db",
                    "method": "vacuum-into",
                    "helper": HELPER,
                    "tmp": TMP,
                    "paired_file": "k3s/server/token",
                }
            ],
        }
    ],
}

TOKEN = b"K10deadbeef::server:secret\n"
TOKEN_SHA = hashlib.sha256(TOKEN).hexdigest()
COPY = b"SQLite format 3\x00 ... pretend this is 19 MB"
COPY_SHA = hashlib.sha256(COPY).hexdigest()


def raw(**overrides):
    out = json.loads(json.dumps(RAW))
    out["jobs"][0]["dumps"][0].update(overrides)
    return out


@pytest.fixture
def config(tmp_path):
    parsed = json.loads(json.dumps(RAW))
    parsed["destination"]["root"] = str(tmp_path)
    return cfg.parse(parsed)


@pytest.fixture
def spec(config):
    return config.jobs[0].dumps[0]


def stored_token(config, content=TOKEN):
    path = config.root / "k3s" / "server" / "token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def payload(**overrides):
    body = {
        "artifact": TMP,
        "source_bytes": 20291584,
        "bytes": 19_000_000,
        "sha256": COPY_SHA,
        "integrity_check": "ok",
        "paired_sha256": TOKEN_SHA,
    }
    body.update(overrides)
    return json.dumps(body).encode()


# --------------------------------------------------------------- invocation


def record_calls(monkeypatch):
    """Capture every execute.run, because the pull follows the dump and the
    last call would otherwise overwrite the one under test."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return execute.Result(argv=argv, returncode=0, stdout=payload(), stderr="")

    monkeypatch.setattr(execute, "run", fake_run)
    return calls


def test_a_helper_is_invoked_through_sudo_and_is_sent_no_program(config, monkeypatch):
    calls = record_calls(monkeypatch)
    with pytest.raises(Exception):
        # The pull is not stubbed; we only care about how the helper was called.
        jobs._take_sqlite_dump(config, config.jobs[0], config.jobs[0].dumps[0], log=lambda _m: None, dry_run=False)

    argv, sent = calls[0]
    assert "sudo -n " + HELPER in " ".join(argv)
    # A helper that reads stdin could be steered; this one must be sent nothing.
    assert sent is None


def test_without_a_helper_the_program_is_still_piped(config, monkeypatch):
    plain = json.loads(json.dumps(RAW))
    plain["destination"]["root"] = str(config.root)
    del plain["jobs"][0]["dumps"][0]["helper"]
    del plain["jobs"][0]["dumps"][0]["paired_file"]
    parsed = cfg.parse(plain)
    calls = record_calls(monkeypatch)
    with pytest.raises(Exception):
        jobs._take_sqlite_dump(parsed, parsed.jobs[0], parsed.jobs[0].dumps[0], log=lambda _m: None, dry_run=False)

    assert calls[0][1] == sqlite_dump.PROGRAM.encode()


# ---------------------------------------------------- the reported contract


def test_the_happy_path_is_accepted(spec):
    assert jobs._sqlite_stats(spec, payload())["integrity_check"] == "ok"


@pytest.mark.parametrize(
    "stdout, match",
    [
        (b"not json at all", "did not print one JSON object"),
        (b'{"artifact": "/x"} trailing', "did not print one JSON object"),
        (b"[1, 2]", "not a JSON object"),
        (payload(source_bytes="20291584"), "not a size"),
        (payload(bytes=-1), "not a size"),
        (payload(bytes=True), "not a size"),
        (payload(bytes=0), "zero-byte copy"),
        (payload(integrity_check="row 4 missing"), "integrity_check"),
        (payload(artifact="relative/path"), "not an absolute path"),
        (payload(artifact="/somewhere/else.db"), "config.yaml says the copy is at"),
        (payload(paired_sha256="nope"), "not a sha256"),
        (payload(sha256="nope"), "not a sha256"),
        (payload(sha256=None), "not a sha256"),
        (b'{"artifact": "x", "artifact": "y"}', "duplicate key"),
        (payload(surprise="a new field nobody taught the runner about"), "expected exactly"),
        (b'{"source_bytes": 1, "bytes": 1, "integrity_check": "ok"}', "expected exactly"),
    ],
)
def test_every_way_of_breaking_the_contract_is_a_job_error(spec, stdout, match):
    with pytest.raises(JobError, match=match):
        jobs._sqlite_stats(spec, stdout)


def test_trailing_whitespace_is_not_a_broken_contract(spec):
    assert jobs._sqlite_stats(spec, payload() + b"\n\n")["bytes"] > 0


# ------------------------------------------------------------- the pairing


def test_a_stored_token_matching_the_dump_is_confirmed(config, spec):
    stored_token(config)
    assert jobs._confirm_pairing(config, spec, json.loads(payload())) == TOKEN_SHA


def test_a_token_stored_from_a_different_moment_fails_the_job(config, spec):
    # `paths` run before `dumps`, so this is the window the helper's own
    # before/after check cannot see.
    stored_token(config, b"K10oldervalue::server:secret\n")
    with pytest.raises(JobError, match="cannot be restored together"):
        jobs._confirm_pairing(config, spec, json.loads(payload()))


def test_a_missing_companion_is_not_silently_accepted(config, spec):
    with pytest.raises(JobError, match="no such file was stored"):
        jobs._confirm_pairing(config, spec, json.loads(payload()))


def test_without_a_paired_file_there_is_nothing_to_confirm(config):
    plain = json.loads(json.dumps(RAW))
    plain["destination"]["root"] = str(config.root)
    del plain["jobs"][0]["dumps"][0]["paired_file"]
    parsed = cfg.parse(plain)
    assert jobs._confirm_pairing(parsed, parsed.jobs[0].dumps[0], json.loads(payload())) is None


# ------------------------------------------------------------ config rules


def test_a_helper_must_be_an_absolute_path():
    with pytest.raises(ConfigError, match="absolute path on the source"):
        cfg.parse(raw(helper="k3s-datastore-snapshot"))


def test_a_paired_file_without_a_helper_is_refused():
    body = raw()
    del body["jobs"][0]["dumps"][0]["helper"]
    with pytest.raises(ConfigError, match="only means something with a `helper`"):
        cfg.parse(body)


def test_a_paired_file_may_not_escape_the_destination():
    with pytest.raises(ConfigError):
        cfg.parse(raw(paired_file="../../etc/passwd"))


# ----------------------------------------------------------- restore text


def test_the_restore_text_recreates_the_directory_the_paths_half_excludes(config, spec):
    from runner import manifest

    text = manifest.restore_dump(config.sources["prd01"], spec, config.root)
    assert "install -d -o root -g root -m 0700 /var/lib/rancher/k3s/server/db" in text
    assert "NOT YET DRILLED" in text
    assert "sha256sum the restored k3s/server/token" in text
    # The runner's own grant on prd01 is `rsync --server --sender`: read-only.
    # A procedure that quietly assumes otherwise fails at step 4 of a disaster.
    assert "AS ROOT ON THE TARGET HOST" in text
    assert "--rsync-path" not in text
    assert "sudo" not in text


# ------------------------------------------- the bytes that actually arrived


def test_the_pulled_copy_must_be_the_one_the_helper_described(tmp_path, spec):
    pulled = tmp_path / "state.db.part"
    pulled.write_bytes(COPY)
    jobs._confirm_artifact(spec, json.loads(payload()), pulled)  # does not raise


def test_an_artifact_replaced_between_the_report_and_the_pull_is_refused(tmp_path, spec):
    # The sudo grant is not exclusive: a second invocation can overwrite the
    # fixed artifact path after the helper has already reported on the first.
    pulled = tmp_path / "state.db.part"
    pulled.write_bytes(b"a different database entirely")
    with pytest.raises(JobError, match="changed between"):
        jobs._confirm_artifact(spec, json.loads(payload()), pulled)


def test_a_piped_program_dump_has_no_artifact_digest_to_confirm(config, tmp_path):
    plain = json.loads(json.dumps(RAW))
    plain["destination"]["root"] = str(config.root)
    del plain["jobs"][0]["dumps"][0]["helper"]
    del plain["jobs"][0]["dumps"][0]["paired_file"]
    parsed = cfg.parse(plain)
    pulled = tmp_path / "n8n.sqlite.part"
    pulled.write_bytes(b"anything")
    jobs._confirm_artifact(parsed.jobs[0].dumps[0], {}, pulled)  # does not raise
