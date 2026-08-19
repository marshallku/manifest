"""Job orchestration: preconditions, isolation, and the unconditional manifest."""

import json

import pytest

from runner import config as cfg, execute, jobs
from runner.errors import JobError

RAW = {
    "version": 1,
    "destination": {"root": "/replaced", "snapshot": {"host": "pve02", "dataset": "tank/backup/hosts"}},
    "sources": {"prd01": {"ssh": "marshall@10.0.0.1", "privilege": "sudo-rsync"}},
    "jobs": [
        {
            "name": "miniflux-cold",
            "source": "prd01",
            "paths": [{"from": "/mnt/hdd/data/miniflux", "to": "miniflux/files"}],
            "precondition": {"container_not_running": "miniflux-db"},
        }
    ],
}


@pytest.fixture
def config(tmp_path):
    raw = json.loads(json.dumps(RAW))
    raw["destination"]["root"] = str(tmp_path)
    return cfg.parse(raw)


def fake_run(returncode=0, stdout="", stderr=""):
    def _run(argv, **kwargs):
        return execute.Result(argv=argv, returncode=returncode, stdout=stdout, stderr=stderr)

    return _run


def silent(_message):
    pass


def test_a_running_container_fails_the_job_rather_than_skipping_it(config, monkeypatch):
    """Skipping is how a stale assumption becomes a silent hole."""
    monkeypatch.setattr(execute, "run", fake_run(stdout="running\n"))
    report = jobs.run_job(config, config.jobs[0], log=silent)

    assert report.status == "failed"
    assert "is 'running'" in report.error
    assert not (config.root / "miniflux" / "files").exists()


def test_an_absent_container_satisfies_the_precondition(config, monkeypatch):
    """`docker ps` exits 0 and prints nothing when there is no such container."""
    monkeypatch.setattr(execute, "run", fake_run(stdout=""))
    monkeypatch.setattr(execute, "check", lambda argv, what, **kw: execute.Result(argv, 0, "", ""))
    report = jobs.run_job(config, config.jobs[0], log=silent)
    assert report.status == "ok"


@pytest.mark.parametrize("state", ["exited", "created", "dead"])
def test_a_dormant_container_satisfies_the_precondition(config, monkeypatch, state):
    monkeypatch.setattr(execute, "run", fake_run(stdout=f"{state}\n"))
    monkeypatch.setattr(execute, "check", lambda argv, what, **kw: execute.Result(argv, 0, "", ""))
    assert jobs.run_job(config, config.jobs[0], log=silent).status == "ok"


@pytest.mark.parametrize("state", ["running", "restarting", "paused", "removing", "something-new"])
def test_any_state_that_is_not_dormant_fails_the_precondition(config, monkeypatch, state):
    """`restarting` and `paused` are as live as `running` for a datadir, and a
    state Docker adds later must fail closed."""
    monkeypatch.setattr(execute, "run", fake_run(stdout=f"{state}\n"))

    def explode(*args, **kwargs):
        raise AssertionError("the transfer must not start")

    monkeypatch.setattr(execute, "check", explode)
    report = jobs.run_job(config, config.jobs[0], log=silent)
    assert report.status == "failed" and state in report.error


def test_a_check_that_could_not_run_is_not_read_as_absence(config, monkeypatch):
    """An unreachable host or a dead docker daemon is not evidence that the
    container is stopped; treating it as such green-lights an rsync of a live
    datadir, which is the single thing this precondition exists to prevent."""
    monkeypatch.setattr(execute, "run", fake_run(returncode=255, stderr="ssh: connect: timed out"))

    def explode(*args, **kwargs):
        raise AssertionError("the transfer must not start on an unverified precondition")

    monkeypatch.setattr(execute, "check", explode)
    report = jobs.run_job(config, config.jobs[0], log=silent)
    assert report.status == "failed"
    assert "could not be checked" in report.error


def test_a_manifest_is_written_even_when_the_job_failed(config, monkeypatch):
    monkeypatch.setattr(execute, "run", fake_run(stdout="running\n"))
    jobs.run_job(config, config.jobs[0], log=silent)

    written = json.loads((config.root / "miniflux" / "manifest.json").read_text())
    assert written["status"] == "failed"
    assert written["job"] == "miniflux-cold"
    assert written["precondition"] == {"container_not_running": "miniflux-db"}
    assert "is 'running'" in written["error"]


def test_a_successful_manifest_records_how_to_restore(config, monkeypatch):
    monkeypatch.setattr(execute, "run", fake_run(stdout=""))
    monkeypatch.setattr(execute, "check", lambda argv, what, **kw: execute.Result(argv, 0, "", ""))
    jobs.run_job(config, config.jobs[0], log=silent)

    written = json.loads((config.root / "miniflux" / "manifest.json").read_text())
    assert written["status"] == "ok"
    artifact = written["artifacts"][0]
    assert artifact["fake_super"] is True
    assert "--fake-super" in artifact["restore"]
    assert artifact["restore"].endswith("marshall@10.0.0.1:/mnt/hdd/data/miniflux/")


def test_a_dry_run_touches_nothing(config, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a dry run must not execute anything")

    monkeypatch.setattr(execute, "run", explode)
    monkeypatch.setattr(execute, "check", explode)
    report = jobs.run_job(config, config.jobs[0], log=silent, dry_run=True)

    assert report.status == "ok"
    assert not (config.root / "miniflux").exists()


def test_one_failing_job_does_not_raise_out_of_the_runner(config, monkeypatch):
    def boom(*args, **kwargs):
        raise JobError("rsync exploded")

    monkeypatch.setattr(execute, "run", fake_run(stdout=""))
    monkeypatch.setattr(execute, "check", boom)
    report = jobs.run_job(config, config.jobs[0], log=silent)
    assert report.status == "failed" and report.error == "rsync exploded"


SQLITE_RAW = {
    "version": 1,
    "destination": {"root": "/replaced", "snapshot": {"host": "pve02", "dataset": "tank/backup/hosts"}},
    "sources": {"prd01": {"ssh": "marshall@10.0.0.1", "privilege": "sudo-rsync"}},
    "jobs": [
        {
            "name": "n8n",
            "source": "prd01",
            "dumps": [
                {
                    "to": "n8n/dump/database.sqlite",
                    "engine": "sqlite",
                    "path": "/data/database.sqlite",
                    "method": "vacuum-into",
                    "tmp": "/tmp/n8n.sqlite",
                }
            ],
        }
    ],
}


def test_a_refused_leftover_temp_file_is_not_then_deleted(tmp_path, monkeypatch):
    """The refusal exists so a human looks at the file. Cleaning it up anyway
    would turn "inspect this" into "fails once, then quietly proceeds"."""
    raw = json.loads(json.dumps(SQLITE_RAW))
    raw["destination"]["root"] = str(tmp_path)
    config = cfg.parse(raw)

    removals = []

    def record(argv, **kwargs):
        removals.append(argv)
        return execute.Result(argv, 0, "", "")

    def refuse(argv, what, **kwargs):
        raise JobError("refusing to run: /tmp/n8n.sqlite already exists")

    monkeypatch.setattr(execute, "run", record)
    monkeypatch.setattr(execute, "check", refuse)

    report = jobs.run_job(config, config.jobs[0], log=silent)

    assert report.status == "failed"
    assert "already exists" in report.error
    assert not any("rm -f" in " ".join(argv) for argv in removals), removals


def test_a_dump_whose_temp_file_could_not_be_removed_is_not_reported_ok(tmp_path, monkeypatch):
    """The leftover would make tomorrow's run fail for no visible reason, so
    the run that caused it is the one that has to say so."""
    raw = json.loads(json.dumps(SQLITE_RAW))
    raw["destination"]["root"] = str(tmp_path)
    config = cfg.parse(raw)

    def rm_fails(argv, **kwargs):
        if "rm -f" in " ".join(argv):
            return execute.Result(argv, 1, "", "rm: Permission denied")
        return execute.Result(argv, 0, "", "")

    def succeed(argv, what, **kwargs):
        if "python3" in " ".join(argv):
            return execute.Result(argv, 0, '{"bytes": 1, "source_bytes": 2, "integrity_check": "ok"}', "")
        (tmp_path / "n8n" / "dump").mkdir(parents=True, exist_ok=True)
        (tmp_path / "n8n" / "dump" / "database.sqlite.part").write_bytes(b"db")
        return execute.Result(argv, 0, "", "")

    monkeypatch.setattr(execute, "run", rm_fails)
    monkeypatch.setattr(execute, "check", succeed)

    report = jobs.run_job(config, config.jobs[0], log=silent)
    assert report.status == "failed"
    assert "could not be removed" in report.error
    # The dump itself was still kept — only the snapshot waits.
    assert (tmp_path / "n8n" / "dump" / "database.sqlite").exists()
