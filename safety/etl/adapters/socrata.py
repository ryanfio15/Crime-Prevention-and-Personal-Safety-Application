"""Shared Socrata SODA plumbing for the Chicago, Seattle and Los Angeles adapters.

Everything here is the API paradigm rather than the city: the app-token header,
retry policy, calendar-month chunking with a truncation guard, CSV decoding, and
reading Socrata *floating* timestamps. What differs per city -- which columns to
request, which field is the occurrence time, how a record maps onto
`NormalizedIncident` -- stays in each city's module (S11).

The reasoning behind each choice is documented where it was first made, in
chicago.py; this module is that code lifted out unchanged so the next two
Socrata cities do not carry a copy each.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, ClassVar

import httpx

from safety.config import settings
from safety.etl.adapters.base import RawChunk, SourceAdapter

log = logging.getLogger(__name__)

# Socrata's ceiling on $limit for a single request.
PAGE_LIMIT = 50_000


class SocrataAdapter(SourceAdapter):
    api_type: ClassVar[str] = "socrata"

    # Set by each city.
    incident_columns: ClassVar[tuple[str, ...]]
    occurred_field: ClassVar[str]
    order_field: ClassVar[str]

    # ------------------------------------------------------------------ fetch

    @property
    def _endpoint(self) -> str:
        return f"{self.config.base_url}/{self.config.incident_dataset}.csv"

    def _headers(self) -> dict[str, str]:
        if settings.socrata_app_token:
            return {"X-App-Token": settings.socrata_app_token}
        return {}

    def _get(self, params: dict[str, str]) -> httpx.Response:
        """GET with bounded exponential backoff; 429 is a "come back", like a 5xx."""
        last_exc: Exception | None = None
        for attempt in range(1, settings.http_max_retries + 1):
            try:
                response = httpx.get(
                    self._endpoint,
                    params=params,
                    headers=self._headers(),
                    timeout=settings.http_timeout_seconds,
                    follow_redirects=True,
                )
                if response.status_code >= 500 or response.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"upstream {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_exc = exc
                backoff = 2.0**attempt
                log.warning(
                    "socrata request failed (attempt %s/%s), retrying in %.0fs: %s",
                    attempt,
                    settings.http_max_retries,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
        raise RuntimeError(f"Socrata request failed after retries: {last_exc}")

    @staticmethod
    def _month_starts(
        since: datetime, until: datetime
    ) -> Iterator[tuple[datetime, datetime]]:
        """Calendar-month windows: deep $offset paging degrades on Socrata, and
        month-partitioned snapshots are what the S8.5 volume check compares."""
        cursor = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cursor < until:
            if cursor.month == 12:
                nxt = cursor.replace(year=cursor.year + 1, month=1)
            else:
                nxt = cursor.replace(month=cursor.month + 1)
            yield cursor, min(nxt, until)
            cursor = nxt

    def fetch_incidents(self, since: datetime, until: datetime) -> Iterator[RawChunk]:
        """One chunk per calendar month, filtered on the occurrence field.

        The bounds are formatted without an offset because the occurrence field
        is a floating local timestamp on every Socrata source this project uses.
        """
        for chunk_start, chunk_end in self._month_starts(since, until):
            params = {
                "$select": ", ".join(self.incident_columns),
                "$where": (
                    f"{self.occurred_field} >= '{floating(chunk_start)}' "
                    f"AND {self.occurred_field} < '{floating(chunk_end)}'"
                ),
                "$order": self.order_field,
                "$limit": str(PAGE_LIMIT),
            }
            response = self._get(params)
            payload = response.content
            record_count = max(payload.count(b"\n") - 1, 0)

            if record_count >= PAGE_LIMIT:
                # A truncated month would look exactly like a quiet one.
                raise RuntimeError(
                    f"{self.source_id}: {chunk_start:%Y-%m} returned {record_count} "
                    f"rows, at or above the $limit ceiling of {PAGE_LIMIT}, so the "
                    "month is probably truncated. Split the chunking finer than "
                    "monthly."
                )

            log.info(
                "%s: fetched %s rows for %s",
                self.source_id,
                record_count,
                chunk_start.strftime("%Y-%m"),
            )
            yield RawChunk(
                name=f"{chunk_start.strftime('%Y-%m')}.csv",
                content_type="text/csv",
                payload=payload,
                request_url=str(response.url),
                fetched_at=datetime.now(timezone.utc),
                record_count=record_count,
                meta={
                    "window_start": chunk_start.isoformat(),
                    "window_end": chunk_end.isoformat(),
                },
            )

    def fetch_boundary(self) -> RawChunk | None:
        """None: the coverage polygon comes from TIGER/Line PLACE (boundary.py)."""
        return None

    # ------------------------------------------------------------------ parse

    def parse_incidents(self, chunk: RawChunk) -> Iterator[dict[str, Any]]:
        text = chunk.payload.decode("utf-8-sig")
        yield from csv.DictReader(io.StringIO(text))

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _to_float(cls, value: Any) -> float | None:
        text = cls._clean(value)
        if text is None:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @classmethod
    def _parse_local(cls, value: Any) -> datetime | None:
        """Read a Socrata floating timestamp: '2026-01-12T20:20:00.000'.

        The value is a local wall clock. It is tagged UTC to keep the column
        timezone-aware and not converted -- the rest of the pipeline treats
        `occurred_at` that way (009_time_of_day.sql).
        """
        text = cls._clean(value)
        if text is None:
            return None
        candidate = text.replace(" ", "T")
        if candidate.endswith("Z"):
            candidate = candidate[:-1]
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _wgs84_or_none(
        latitude: float | None, longitude: float | None
    ) -> tuple[float | None, float | None, str]:
        """The published pair if usable; (0, 0) means absent, not the Gulf of Guinea."""
        if (
            latitude is not None
            and longitude is not None
            and not (latitude == 0 and longitude == 0)
            and abs(latitude) <= 90.0
            and abs(longitude) <= 180.0
        ):
            return latitude, longitude, "published_wgs84"
        return None, None, "missing"


def floating(value: datetime) -> str:
    """Format a bound as a Socrata floating timestamp, with no offset."""
    return value.strftime("%Y-%m-%dT%H:%M:%S")
