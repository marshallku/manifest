"""The transport layer: what actually gets executed locally."""

import pytest

from runner import source as transport
from runner.config import Source
from runner.errors import JobError

SSH = Source("prd01", "ssh", ssh="marshall@10.0.0.1", privilege="sudo-rsync")
LOCAL = Source("here", "local")
K8S = Source("k3s", "kubectl")


def test_ssh_does_not_use_a_double_dash_terminator():
    """`ssh host -- cmd` runs a remote command literally beginning with `--`."""
    argv = transport.wrap(SSH, ["docker", "ps"])
    assert "--" not in argv
    assert argv[-2] == "marshall@10.0.0.1"
    assert argv[-1] == "docker ps"


def test_the_remote_command_is_one_shell_safe_string():
    argv = transport.wrap(SSH, ["sh", "-c", "echo $HOME; rm -rf /"])
    assert argv[-1] == "sh -c 'echo $HOME; rm -rf /'"


def test_a_local_source_runs_the_argv_unchanged():
    assert transport.wrap(LOCAL, ["docker", "ps"]) == ["docker", "ps"]


def test_kubectl_does_use_a_double_dash_terminator():
    argv = transport.exec_in_pod(K8S, "infisical/postgres-0", "postgres", "echo hi")
    assert argv[:7] == ["kubectl", "exec", "-n", "infisical", "postgres-0", "-c", "postgres"]
    assert argv[7] == "--"


def test_a_pod_without_a_namespace_is_refused():
    with pytest.raises(JobError, match="namespace"):
        transport.exec_in_pod(K8S, "postgres-0", None, "echo hi")


def test_pulling_over_ssh_preserves_ownership_without_privilege():
    argv = transport.rsync_argv(SSH, src="/srv/blog", dest="/mnt/backup/hosts/blog/files", exclude=("/db/",))
    assert "--fake-super" in argv
    assert "--rsync-path=sudo rsync" in argv
    assert "--exclude=/db/" in argv
    assert argv[-2] == "marshall@10.0.0.1:/srv/blog/"
    assert argv[-1] == "/mnt/backup/hosts/blog/files/"


def test_pulling_locally_needs_neither():
    argv = transport.rsync_argv(LOCAL, src="/srv/x", dest="/dst")
    assert "--fake-super" not in argv
    assert not any(a.startswith("--rsync-path") for a in argv)


def test_the_destination_mirrors_rather_than_accumulates():
    argv = transport.rsync_argv(SSH, src="/srv/x", dest="/dst")
    assert "--delete" in argv and "--delete-excluded" in argv


def test_xattrs_are_requested_because_fake_super_stores_ownership_there():
    argv = transport.rsync_argv(SSH, src="/srv/x", dest="/dst")
    assert "--xattrs" in argv and "--numeric-ids" in argv


def test_a_kubectl_source_has_nothing_to_pull():
    with pytest.raises(JobError, match="no filesystem"):
        transport.rsync_argv(K8S, src="/srv/x", dest="/dst")
