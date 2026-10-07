"""safety.etl.run.is_due: pure cadence arithmetic. It returns (decision, reason)."""

from __future__ import annotations

import pytest

from safety.etl.run import is_due


@pytest.mark.parametrize(
    ("cadence", "days", "expected"),
    [
        ("daily", None, True),  # never pulled
        ("no-such-cadence", 0.0, True),  # unknown cadence pulls rather than skips
        (None, 0.0, True),
        ("daily", 0.5, False),
        ("daily", 0.9, True),  # threshold is 0.8 of a day (19.2 h)
        ("weekly", 5.0, False),
        ("weekly", 6.0, True),
        ("rolling", 0.01, True),
    ],
)
def test_is_due(cadence, days, expected):
    ok, reason = is_due(cadence, days)
    assert ok is expected
    assert isinstance(reason, str) and reason
