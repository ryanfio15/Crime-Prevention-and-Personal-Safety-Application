"""safety.joblock: the cluster-wide ETL advisory lock that replaces the host flock."""

from __future__ import annotations

import sys

import pytest

from safety import joblock


def test_parser_strips_the_separator():
    args = joblock.build_parser().parse_args(["--wait-seconds", "3", "--", "echo", "-n", "x"])
    assert args.wait_seconds == 3
    assert args.command == ["--", "echo", "-n", "x"] or args.command == ["echo", "-n", "x"]


def test_no_command_is_a_usage_error(capsys):
    assert joblock.main(["--"]) == 2


@pytest.mark.db
def test_exit_code_passes_through(db_conn):
    code = joblock.run_locked([sys.executable, "-c", "raise SystemExit(3)"], wait_seconds=5)
    assert code == 3


@pytest.mark.db
def test_a_held_lock_times_out_with_tempfail(db_conn):
    holder = joblock._connect()
    try:
        assert holder.execute(
            "SELECT pg_try_advisory_lock(%s)", (joblock.ETL_LOCK_KEY,)
        ).fetchone()[0]
        with pytest.raises(TimeoutError):
            with joblock.etl_lock(wait_seconds=0):
                pass
        code = joblock.run_locked([sys.executable, "-c", "pass"], wait_seconds=0)
        assert code == joblock.EX_TEMPFAIL
    finally:
        holder.close()
    # Released with the session: the next caller gets it at once.
    with joblock.etl_lock(wait_seconds=0):
        pass
