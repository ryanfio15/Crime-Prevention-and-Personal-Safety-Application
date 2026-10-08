"""Planning the history load (safety.etl.run history, migration 018)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from safety import PIPELINE_VERSION, ops
from safety.etl import run

UTC = timezone.utc


def test_slices_walk_back_to_the_floor_newest_first():
    slices = run.history_slices(datetime(2024, 10, 1, tzinfo=UTC), date(2022, 1, 1), 6)
    assert slices[0][1] == datetime(2024, 10, 2, tzinfo=UTC)
    # Contiguous (each slice ends a day past where the next older one starts),
    # newest first, and the last one starts exactly on the floor.
    for newer, older in zip(slices, slices[1:]):
        assert older[1] > newer[0] >= older[0]
    assert slices[-1][0] == datetime(2022, 1, 1, tzinfo=UTC)
    assert all(since < until for since, until in slices)


def test_slice_length_follows_the_months_asked_for():
    three = run.history_slices(datetime(2024, 10, 1, tzinfo=UTC), date(2001, 1, 1), 3)
    twelve = run.history_slices(datetime(2024, 10, 1, tzinfo=UTC), date(2001, 1, 1), 12)
    # 2001-01-01 to 2024-10-01 is 23 years and 9 months.
    assert len(three) == 95
    assert len(twelve) == 24
    assert all((until - since).days <= 3 * 31 + 2 for since, until in three)
    assert all((until - since).days <= 366 + 2 for since, until in twelve)


def test_nothing_left_once_coverage_reaches_the_floor():
    assert run.history_slices(datetime(2008, 1, 1, tzinfo=UTC), date(2008, 1, 1), 6) == []
    assert run.history_slices(datetime(2007, 6, 1, tzinfo=UTC), date(2008, 1, 1), 6) == []


def test_chicago_uses_short_slices():
    assert run.HISTORY_SLICE_MONTHS["chi"] < run.DEFAULT_HISTORY_SLICE_MONTHS


def _state(**overrides):
    state = {
        "last_incident_pull_at": datetime(2026, 10, 1, tzinfo=UTC),
        "has_census": True,
        "has_hourly": True,
        "has_clock_hours": True,
        "gold_refreshed_at": datetime(2026, 10, 2, tzinfo=UTC),
        "gold_pipeline_version": PIPELINE_VERSION,
        "history_enabled": True,
        "history_start_date": date(2008, 1, 1),
        "covered_from": datetime(2024, 9, 30, tzinfo=UTC),
    }
    state.update(overrides)
    return state


def test_history_is_planned_while_coverage_is_short_of_the_start():
    steps = ops.plan_from_state(_state(), "sea")
    assert [s.task for s in steps] == ["history"]
    assert steps[0].argv[:3] == ("history", "--city", "sea")
    assert "--max-minutes" in steps[0].argv


def test_history_is_not_planned_when_disabled_or_done():
    assert ops.plan_from_state(_state(history_enabled=False), "sea") == []
    assert ops.plan_from_state(_state(history_start_date=None), "sea") == []
    done = _state(covered_from=datetime(2008, 1, 1, tzinfo=UTC))
    assert ops.plan_from_state(done, "sea") == []


def test_history_waits_for_the_first_backfill():
    fresh = _state(last_incident_pull_at=None, covered_from=None, has_census=True)
    assert "history" not in [s.task for s in ops.plan_from_state(fresh, "sea")]


def test_history_runs_after_a_gold_rebuild_in_the_same_plan():
    stale = _state(gold_pipeline_version="phase2.0.0")
    assert [s.task for s in ops.plan_from_state(stale, "sea")] == ["gold", "history"]


class _Cursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args):
        pass

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return _Cursor(self.row)


def _last(status, version, hours=1.0):
    return {"status": status, "started_at": None, "error": None,
            "pipeline_version": version, "hours_ago": hours}


GOLD = ops.Step(task="gold", city="phl", reason="x", argv=("gold", "--city", "phl"))
HISTORY = ops.Step(task="history", city="phl", reason="x", argv=("history", "--city", "phl"))


def test_success_under_this_pipeline_still_blocks_a_repeat():
    assert ops._cooldown_block(_Conn(_last("succeeded", PIPELINE_VERSION)), GOLD, 6) is not None


def test_success_under_an_older_pipeline_does_not_block():
    assert ops._cooldown_block(_Conn(_last("succeeded", "phase0.0.0")), GOLD, 6) is None


def test_a_recent_failure_still_blocks_whatever_the_version():
    assert ops._cooldown_block(_Conn(_last("failed", "phase0.0.0")), GOLD, 6) is not None


def test_history_success_never_blocks():
    assert ops._cooldown_block(_Conn(_last("succeeded", PIPELINE_VERSION)), HISTORY, 6) is None
