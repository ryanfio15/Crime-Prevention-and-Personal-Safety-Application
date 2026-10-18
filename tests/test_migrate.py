"""Migration checksums and the migrate advisory lock (F16)."""

from __future__ import annotations

import psycopg
import pytest

from safety import migrate


def test_checksum_ignores_line_endings(tmp_path):
    lf = tmp_path / "lf.sql"
    crlf = tmp_path / "crlf.sql"
    lf.write_bytes(b"CREATE TABLE t (a int);\n-- note\n")
    crlf.write_bytes(b"CREATE TABLE t (a int);\r\n-- note\r\n")
    assert migrate.migration_checksum(lf) == migrate.migration_checksum(crlf)


def test_checksum_changes_with_content(tmp_path):
    a = tmp_path / "a.sql"
    b = tmp_path / "b.sql"
    a.write_text("SELECT 1;\n")
    b.write_text("SELECT 2;\n")
    assert migrate.migration_checksum(a) != migrate.migration_checksum(b)


def _paths():
    return sorted(migrate.MIGRATIONS_DIR.glob("*.sql"))


@pytest.mark.db
def test_every_applied_migration_has_its_files_checksum(db_conn):
    # CI applies main's migrations with main's (checksum-less) migrate first, so
    # this also proves the adoption path an existing host takes.
    rows = db_conn.execute(
        "SELECT filename, checksum FROM public.schema_migration"
    ).fetchall()
    db_conn.rollback()
    files = {p.name: p for p in _paths()}
    assert rows
    for r in rows:
        assert r["filename"] in files
        assert r["checksum"] == migrate.migration_checksum(files[r["filename"]]), r["filename"]


@pytest.mark.db
def test_an_edited_applied_migration_is_refused(db_conn):
    victim = _paths()[0].name
    try:
        db_conn.execute(
            "UPDATE public.schema_migration SET checksum = %s WHERE filename = %s",
            ("0" * 64, victim),
        )
        with pytest.raises(migrate.MigrationChecksumError, match=victim):
            migrate.verify_checksums(db_conn, _paths())
    finally:
        db_conn.rollback()
    row = db_conn.execute(
        "SELECT checksum FROM public.schema_migration WHERE filename = %s", (victim,)
    ).fetchone()
    db_conn.rollback()
    assert row["checksum"] != "0" * 64


@pytest.mark.db
def test_migration_lock_waits_for_another_migrate(db_conn):
    from safety.config import settings

    other = psycopg.connect(settings.dsn, autocommit=True)
    try:
        other.execute("SELECT pg_advisory_lock(%s)", (migrate._MIGRATE_LOCK_KEY,))
        with pytest.raises(TimeoutError):
            with migrate.migration_lock(db_conn, wait_seconds=1, poll=0.2):
                pass
    finally:
        other.close()
    with migrate.migration_lock(db_conn, wait_seconds=5, poll=0.2):
        held = db_conn.execute(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        ).fetchone()["n"]
    db_conn.rollback()
    assert held == 1
