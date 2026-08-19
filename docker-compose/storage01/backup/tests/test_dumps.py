"""The dump snippets carry two invariants that silently break backups."""

import shlex

import pytest

from runner import dumps
from runner.config import DumpSpec
from runner.errors import ConfigError


def mysql(**patch):
    spec = dict(
        dest="d/x.sql.gz",
        engine="mariadb-dump",
        container="nextcloud_db",
        database="clouddb",
        args=("--single-transaction", "--protocol=socket"),
        auth={"password_env": "MARIADB_ROOT_PASSWORD"},
    )
    spec.update(patch)
    return DumpSpec(**spec)


def test_the_password_is_a_variable_reference_not_a_value():
    """The runner must never hold the secret: the container's shell expands it."""
    snippet = dumps.build(mysql()).snippet
    assert 'MYSQL_PWD="${MARIADB_ROOT_PASSWORD:?' in snippet
    assert "MYSQL_PWD=$MARIADB" not in snippet


def test_an_unset_credential_fails_loudly_rather_than_dumping_without_one():
    snippet = dumps.build(mysql()).snippet
    assert ":?MARIADB_ROOT_PASSWORD is not set in this container}" in snippet


def test_nothing_is_piped():
    """`set -o pipefail` is an illegal option in dash, which is blog-database's
    /bin/sh — so a pipe here would silently mask a truncated dump."""
    for spec in (mysql(), mysql(engine="pg_dump", auth={"password_env": "P", "username_env": "U", "database_env": "D"}, database=None)):
        assert "|" not in dumps.build(spec).snippet


def test_declared_args_survive_in_order():
    snippet = dumps.build(mysql()).snippet
    assert "--single-transaction --protocol=socket" in snippet
    assert snippet.rstrip().endswith("clouddb")


def test_mysql_defaults_to_root_when_no_username_is_declared():
    assert " -uroot " in dumps.build(mysql()).snippet


def test_postgres_reads_user_and_database_from_the_container_too():
    spec = DumpSpec(
        dest="d/x.sql.gz",
        engine="pg_dump",
        container="immich_postgres",
        auth={"username_env": "POSTGRES_USER", "password_env": "POSTGRES_PASSWORD", "database_env": "POSTGRES_DB"},
    )
    snippet = dumps.build(spec).snippet
    assert 'PGPASSWORD="${POSTGRES_PASSWORD:?' in snippet
    assert '-U "${POSTGRES_USER:?' in snippet
    assert '-d "${POSTGRES_DB:?' in snippet


def test_mongodump_credentials_stay_inside_the_container():
    spec = DumpSpec(
        dest="d/m.archive.gz",
        engine="mongodump",
        container="blog-database",
        args=("--archive", "--gzip"),
        auth={"username_env": "MONGO_INITDB_ROOT_USERNAME", "password_env": "MONGO_INITDB_ROOT_PASSWORD"},
    )
    snippet = dumps.build(spec).snippet
    assert '-u "${MONGO_INITDB_ROOT_USERNAME:?' in snippet
    assert '-p "${MONGO_INITDB_ROOT_PASSWORD:?' in snippet
    assert "--authenticationDatabase admin" in snippet


def test_a_dump_without_a_password_is_refused():
    with pytest.raises(ConfigError, match="password_env"):
        dumps.build(mysql(auth={}))


def test_sqlite_is_not_a_streaming_engine():
    with pytest.raises(ConfigError, match="does not stream"):
        dumps.build(DumpSpec(dest="a/b.sqlite", engine="sqlite", path="/x", method="vacuum-into", tmp="/t"))


def test_every_snippet_is_a_single_shell_word_when_quoted():
    """It travels as one argv element through `sh -c` and, over ssh, shlex.join."""
    snippet = dumps.build(mysql()).snippet
    assert shlex.split(shlex.quote(snippet)) == [snippet]
