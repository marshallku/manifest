"""The VACUUM INTO program, run for real against a live WAL database.

No network and nothing installed: it is the same stdlib sqlite3 the source
host has, which is the entire reason this approach was chosen.
"""

import json
import os
import sqlite3
import subprocess
import sys

from runner.sqlite_dump import PROGRAM


def make_db(path, rows=1000):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    con.commit()
    return con  # left OPEN, so the copy is taken from a live database


def run(src, tmp):
    return subprocess.run(
        [sys.executable, "-", str(src), str(tmp)],
        input=PROGRAM.encode(),
        capture_output=True,
    )


def test_copies_a_live_wal_database_consistently(tmp_path):
    src, tmp = tmp_path / "live.sqlite", tmp_path / "work" / "copy.sqlite"
    writer = make_db(src)
    # An uncommitted write must not appear in the copy.
    writer.execute("INSERT INTO t (v) VALUES ('uncommitted')")

    result = run(src, tmp)
    assert result.returncode == 0, result.stderr.decode()

    stats = json.loads(result.stdout)
    assert stats["integrity_check"] == "ok"
    assert stats["bytes"] > 0

    copy = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 1000


def test_the_source_is_opened_read_only(tmp_path):
    """A backup must not be able to modify what it is backing up."""
    src, tmp = tmp_path / "live.sqlite", tmp_path / "copy.sqlite"
    make_db(src, rows=10)
    before = src.stat().st_mtime_ns
    assert run(src, tmp).returncode == 0
    assert src.stat().st_mtime_ns == before


def test_refuses_a_leftover_temp_file_and_says_why(tmp_path):
    src, tmp = tmp_path / "live.sqlite", tmp_path / "copy.sqlite"
    make_db(src, rows=10)
    tmp.write_bytes(b"leftover from a killed run")

    result = run(src, tmp)
    assert result.returncode != 0
    assert b"already exists" in result.stderr
    assert tmp.read_bytes() == b"leftover from a killed run"  # not clobbered


def test_refuses_when_the_copy_would_not_fit(tmp_path, monkeypatch):
    src, tmp = tmp_path / "live.sqlite", tmp_path / "copy.sqlite"
    make_db(src, rows=10)
    program = PROGRAM.replace("free = st.f_bavail * st.f_frsize", "free = 0")
    result = subprocess.run(
        [sys.executable, "-", str(src), str(tmp)], input=program.encode(), capture_output=True
    )
    assert result.returncode != 0
    assert b"insufficient space" in result.stderr
    assert not tmp.exists()


def test_a_corrupt_source_fails_instead_of_producing_an_empty_copy(tmp_path):
    src, tmp = tmp_path / "live.sqlite", tmp_path / "copy.sqlite"
    make_db(src, rows=10).close()  # closing checkpoints and removes the -wal
    src.write_bytes(b"not a database" + src.read_bytes()[14:])

    result = run(src, tmp)
    assert result.returncode != 0
    assert not tmp.exists()


def test_a_failure_after_the_copy_exists_still_cleans_it_up(tmp_path):
    """A half-written multi-gigabyte file left behind every night is its own
    outage, so the failure path has to delete as reliably as the success path.

    The only failure that happens *after* the copy is on disk is the integrity
    check, so that is the branch forced here."""
    src, tmp = tmp_path / "live.sqlite", tmp_path / "copy.sqlite"
    make_db(src, rows=10)
    program = PROGRAM.replace('if result != "ok":', "if True:")

    result = subprocess.run(
        [sys.executable, "-", str(src), str(tmp)], input=program.encode(), capture_output=True
    )
    assert result.returncode != 0
    assert b"integrity_check" in result.stderr
    assert not tmp.exists()
