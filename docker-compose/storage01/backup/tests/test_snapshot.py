from datetime import UTC, datetime

import pytest

from runner import config as cfg, snapshot
from runner.errors import JobError

CONFIG = cfg.parse(
    {
        "version": 1,
        "destination": {"root": "/mnt/backup/hosts", "snapshot": {"host": "pve02", "dataset": "tank/backup/hosts"}},
        "sources": {"here": {"local": True}},
        "jobs": [{"name": "j", "source": "here", "paths": [{"from": "/a", "to": "j/files"}]}],
    }
)


def test_the_snapshot_name_is_a_sortable_utc_stamp():
    assert snapshot.name_for(datetime(2026, 8, 19, 3, 4, 5, tzinfo=UTC)) == "backup-20260819T030405Z"


def test_the_request_carries_only_the_name():
    """pve02's forced command pins the dataset; the client cannot choose it."""
    argv = snapshot.argv_for(CONFIG, "backup-20260819T030405Z")
    assert argv[-2:] == ["backupsnap@pve02", "backup-20260819T030405Z"]
    assert "tank/backup/hosts" not in " ".join(argv)


def test_a_name_that_is_not_a_stamp_is_refused_before_it_is_sent():
    with pytest.raises(JobError, match="not of the form"):
        snapshot.argv_for(CONFIG, "backup-20260819T030405Z; zfs destroy -r tank")


def test_a_snapshot_the_pool_never_took_is_not_reported_as_success(tmp_path):
    """The ssh exit code comes from pve02's forced command, not from the pool."""
    with pytest.raises(JobError, match="does not exist"):
        snapshot.confirm(tmp_path, "backup-20260819T030405Z")


def test_confirmation_looks_under_the_destination_root(tmp_path):
    name = "backup-20260819T030405Z"
    (tmp_path / ".zfs" / "snapshot" / name).mkdir(parents=True)
    snapshot.confirm(tmp_path, name)  # does not raise


def test_a_file_where_the_snapshot_should_be_is_not_a_snapshot(tmp_path):
    name = "backup-20260819T030405Z"
    (tmp_path / ".zfs" / "snapshot").mkdir(parents=True)
    (tmp_path / ".zfs" / "snapshot" / name).write_text("")
    with pytest.raises(JobError, match="does not exist"):
        snapshot.confirm(tmp_path, name)
