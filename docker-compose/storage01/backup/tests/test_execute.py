"""Nothing lands at the real path until every exit code has been checked."""

import gzip

import pytest

from runner import execute
from runner.errors import JobError


def producer(script):
    return ["sh", "-c", script]


def test_a_successful_dump_is_written_and_valid(tmp_path):
    dest = tmp_path / "d" / "x.sql.gz"
    size = execute.stream_to_file(producer("echo 'CREATE TABLE t;'"), dest, compress=True)
    assert size > 0
    assert gzip.decompress(dest.read_bytes()) == b"CREATE TABLE t;\n"


def test_a_producer_that_dies_halfway_does_not_leave_a_dump(tmp_path):
    """This is the pipefail failure in disguise: gzip succeeds on a truncated
    stream, so only the producer's own exit code can tell the difference."""
    dest = tmp_path / "x.sql.gz"
    with pytest.raises(JobError, match="exited 1"):
        execute.stream_to_file(producer("echo 'INSERT INTO t'; exit 1"), dest, compress=True)
    assert not dest.exists()
    assert not (tmp_path / ("x.sql.gz" + execute.PART_SUFFIX)).exists()


def test_stderr_from_the_failing_producer_is_reported(tmp_path):
    with pytest.raises(JobError, match="access denied"):
        execute.stream_to_file(
            producer("echo 'access denied' >&2; exit 2"), tmp_path / "x.sql.gz", compress=True
        )


def test_an_empty_dump_is_refused(tmp_path):
    dest = tmp_path / "x.sql.gz"
    with pytest.raises(JobError, match="empty"):
        execute.stream_to_file(producer("true"), dest, compress=False)
    assert not dest.exists()


def test_an_already_compressed_stream_is_not_compressed_again(tmp_path):
    """mongodump --gzip already emits a compressed archive."""
    dest = tmp_path / "m.archive.gz"
    payload = gzip.compress(b"pretend mongo archive")
    execute.stream_to_file(
        ["sh", "-c", f"printf %s {payload.hex()} | xxd -r -p"], dest, compress=False
    )
    assert dest.read_bytes() == payload


def test_a_partial_file_from_an_earlier_run_is_replaced_not_appended(tmp_path):
    dest = tmp_path / "x.sql.gz"
    (tmp_path / ("x.sql.gz" + execute.PART_SUFFIX)).write_bytes(b"junk from a killed run")
    execute.stream_to_file(producer("echo fresh"), dest, compress=True)
    assert gzip.decompress(dest.read_bytes()) == b"fresh\n"


def test_tree_size_counts_what_is_actually_there(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one").write_bytes(b"12345")
    (tmp_path / "two").write_bytes(b"123")
    assert execute.tree_size(tmp_path) == (8, 2)
