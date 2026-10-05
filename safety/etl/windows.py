"""Fetch-window arithmetic, shared by every source (design doc S8.2, S8.3).

This lived in the Philadelphia adapter, which was the wrong home for it: the
arithmetic is the same everywhere, and the one thing that genuinely does differ
per source -- how far back a load is allowed to reach -- is registry
configuration rather than adapter code.

Note the distinction between this module and `gold.resolve_windows`. These are
*fetch* bounds: which records to ask the source for. Those are *reporting*
windows, anchored to the newest date actually reported, and derived from
`occurred_local_date` after the fact. Loading a few extra days here is harmless;
the rollups never see them unless they fall inside a reporting window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    # Type-checking only, deliberately. A runtime import would close a cycle:
    # importing this module would import the adapters package, whose __init__
    # imports philadelphia, which imports this module back before
    # `backfill_window` exists. Nothing here needs the class at runtime.
    from safety.etl.adapters.base import SourceConfig

log = logging.getLogger(__name__)


def months_before(now: datetime, months: int) -> datetime:
    """`now` less `months` calendar months, snapped to the first of the month."""
    year = now.year - (months // 12)
    month = now.month - (months % 12)
    if month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


def backfill_window(
    months: int, config: "SourceConfig | None" = None
) -> tuple[datetime, datetime]:
    """Trailing-window fetch bounds, padded a day each side.

    Chunk boundaries are UTC while the sources report local dates, so the range
    is widened slightly at both ends rather than risking a dropped day at a
    month boundary.

    `config.backfill_start_date` is a floor, and it is the reason this takes a
    config at all. Two of the six sources have a hard classification break
    behind them -- Seattle's May 2019 RMS/NIBRS migration and Los Angeles's
    March 2024 dataset freeze (S4) -- and records from either side of one are not
    a continuous series (S7.3). A widened window, or a deeper historical load,
    must not cross one silently.
    """
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=1)
    since = months_before(now, months) - timedelta(days=1)

    floor = getattr(config, "backfill_start_date", None)
    if floor is not None:
        floor_ts = datetime(floor.year, floor.month, floor.day, tzinfo=timezone.utc)
        if floor_ts > since:
            log.info(
                "%s: backfill floor %s clamps the requested %s-month window "
                "(would have started %s)",
                config.source_id if config else "?",
                floor.isoformat(),
                months,
                since.date().isoformat(),
            )
            since = floor_ts
    return since, until
