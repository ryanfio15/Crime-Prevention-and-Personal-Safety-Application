"""Planning safety.ops steps: retention (one rolling year), the first load, and
the cooldown rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from safety import PIPELINE_VERSION, ops
from safety.etl import retention

UTC = timezone.utc


def _state(**overrides):
    keep_from = retention.cutoff("America/New_York")
    state = {
        "last_incident_pull_at": datetime(2026, 10, 1, tzinfo=UTC),
        "has_census": True,
        "has_hourly": True,
        "has_dow": True,
        "has_clock_hours": True,
        "gold_refreshed_at": datetime.now(UTC) + timedelta(days=1),
        "gold_pipeline_version": PIPELINE_VERSION,
        "timezone": "America/New_York",
        "oldest_incident": keep_from,
    }
    state.update(overrides)
    return state


def test_a_city_inside_its_year_plans_nothing():
    assert ops.plan_from_state(_state(), "phl") == []


def test_incidents_past_the_year_plan_a_prune():
    old = _state(oldest_incident=date(2006, 1, 1))
    steps = ops.plan_from_state(old, "phl")
    assert [s.task for s in steps] == ["retain"]
    assert steps[0].argv == ("prune", "--city", "phl")
    assert "2006-01-01" in steps[0].reason


def test_one_day_past_the_cutoff_is_enough():
    keep_from = retention.cutoff("America/New_York")
    steps = ops.plan_from_state(_state(oldest_incident=keep_from - timedelta(days=1)), "phl")
    assert [s.task for s in steps] == ["retain"]


def test_a_city_with_no_incidents_plans_no_prune():
    assert ops.plan_from_state(_state(oldest_incident=None), "phl") == []


def test_prune_follows_a_gold_rebuild_in_the_same_plan():
    stale = _state(gold_pipeline_version="phase2.0.0", oldest_incident=date(2020, 1, 1))
    assert [s.task for s in ops.plan_from_state(stale, "phl")] == ["gold", "retain"]


def test_first_load_builds_gold_once_after_census():
    fresh = _state(
        last_incident_pull_at=None,
        has_census=False,
        has_hourly=False,
        has_dow=False,
        has_clock_hours=False,
        gold_refreshed_at=None,
        oldest_incident=None,
    )
    steps = ops.plan_from_state(fresh, "phl")
    assert [s.task for s in steps] == ["backfill", "census", "gold"]
    assert "--skip-gold" in steps[0].argv
    # The time-of-day layers ride the one gold build.
    assert "--skip-hourly" not in steps[2].argv


def test_first_load_with_census_already_loaded_builds_gold_in_the_backfill():
    fresh = _state(
        last_incident_pull_at=None,
        has_hourly=False,
        has_dow=False,
        has_clock_hours=False,
        gold_refreshed_at=None,
        oldest_incident=None,
    )
    steps = ops.plan_from_state(fresh, "phl")
    assert [s.task for s in steps] == ["backfill"]
    assert "--skip-gold" not in steps[0].argv


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
RETAIN = ops.Step(task="retain", city="phl", reason="x", argv=("prune", "--city", "phl"))


def test_success_under_this_pipeline_still_blocks_a_repeat():
    assert ops._cooldown_block(_Conn(_last("succeeded", PIPELINE_VERSION)), GOLD, 6) is not None


def test_success_under_an_older_pipeline_does_not_block():
    assert ops._cooldown_block(_Conn(_last("succeeded", "phase0.0.0")), GOLD, 6) is None


def test_a_recent_failure_still_blocks_whatever_the_version():
    assert ops._cooldown_block(_Conn(_last("failed", "phase0.0.0")), GOLD, 6) is not None


def test_retain_success_never_blocks():
    # The cutoff moves every day, so yesterday's success is not a loop.
    assert ops._cooldown_block(_Conn(_last("succeeded", PIPELINE_VERSION)), RETAIN, 6) is None


def test_missing_day_factor_plans_an_hourly_run():
    # A city whose hourly layer predates gold.cell_dow_factor (022).
    steps = ops.plan_from_state(_state(has_dow=False), "phl")
    hourly = [s for s in steps if s.task == "hourly"]
    assert len(hourly) == 1 and "day-of-week" in hourly[0].reason


def test_missing_day_factor_alone_never_plans_for_a_city_without_hours():
    # No hourly layer and no clock hours: nothing to rebuild, so no loop.
    steps = ops.plan_from_state(_state(has_hourly=False, has_clock_hours=False, has_dow=False), "phl")
    assert not [s for s in steps if s.task == "hourly"]
