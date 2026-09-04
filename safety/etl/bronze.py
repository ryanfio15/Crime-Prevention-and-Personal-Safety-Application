"""Bronze layer -- raw snapshots stored exactly as received (design doc S5, S9.2).

Phase 1 writes to the local filesystem behind a narrow interface, because the
production target is versioned S3-compatible object storage: write-once,
read-rarely, kept for auditability ("what did the city actually publish on this
date") and for reprocessing history when standardization logic changes.

Layout mirrors the partitioning the pipeline reasons in:

    <root>/source_id=phl/dataset=incidents_part1_part2/pull_date=2026-09-03/
        pull_000012/
            manifest.json
            2024-09.csv.gz
            2024-10.csv.gz
            ...
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from safety.config import settings
from safety.etl.adapters.base import RawChunk

log = logging.getLogger(__name__)


class BronzePull(Protocol):
    """Handle to one pull's snapshot directory."""

    uri: str

    def write_chunk(self, chunk: RawChunk) -> str: ...

    def write_manifest(self, manifest: dict[str, Any]) -> str: ...

    def iter_chunks(self) -> Iterator[tuple[str, bytes]]: ...

    @property
    def total_bytes(self) -> int: ...


@dataclass
class LocalBronzePull:
    directory: Path
    _bytes_written: int = 0

    @property
    def uri(self) -> str:
        return self.directory.resolve().as_uri()

    @property
    def total_bytes(self) -> int:
        return self._bytes_written

    def write_chunk(self, chunk: RawChunk) -> str:
        # Chunks are stored gzipped but otherwise byte-identical to the
        # response body: no parsing, no reordering, no column pruning.
        path = self.directory / f"{chunk.name}.gz"
        compressed = gzip.compress(chunk.payload)
        path.write_bytes(compressed)
        self._bytes_written += len(compressed)
        return path.resolve().as_uri()

    def write_manifest(self, manifest: dict[str, Any]) -> str:
        path = self.directory / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        return path.resolve().as_uri()

    def iter_chunks(self) -> Iterator[tuple[str, bytes]]:
        """Replay a stored snapshot, for reprocessing without re-fetching (S5)."""
        for path in sorted(self.directory.glob("*.gz")):
            yield path.name[: -len(".gz")], gzip.decompress(path.read_bytes())


class LocalBronzeStore:
    """Filesystem stand-in for the S3-compatible bronze bucket."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.bronze_root)

    def _pull_dir(self, source_id: str, dataset: str, pull_id: int, pull_date: date) -> Path:
        return (
            self.root
            / f"source_id={source_id}"
            / f"dataset={dataset}"
            / f"pull_date={pull_date.isoformat()}"
            / f"pull_{pull_id:06d}"
        )

    def open_pull(
        self, source_id: str, dataset: str, pull_id: int, pull_date: date | None = None
    ) -> LocalBronzePull:
        directory = self._pull_dir(source_id, dataset, pull_id, pull_date or date.today())
        directory.mkdir(parents=True, exist_ok=True)
        log.info("bronze pull directory: %s", directory)
        return LocalBronzePull(directory=directory)

    def open_existing(self, uri: str) -> LocalBronzePull:
        """Reopen a stored snapshot by the URI recorded in etl.pull_run."""
        path = Path.from_uri(uri) if uri.startswith("file:") else Path(uri)
        if not path.exists():
            raise FileNotFoundError(f"no bronze snapshot at {uri}")
        return LocalBronzePull(directory=path)


def build_manifest(
    *,
    source_id: str,
    dataset: str,
    pull_id: int,
    mode: str,
    since: datetime | None,
    until: datetime | None,
    chunks: list[RawChunk],
    pipeline_version: str,
    crosswalk_version: str,
    attribution: str,
) -> dict[str, Any]:
    """Auditability record written alongside the raw payloads."""
    return {
        "source_id": source_id,
        "dataset": dataset,
        "pull_id": pull_id,
        "mode": mode,
        "requested_window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "pipeline_version": pipeline_version,
        "crosswalk_version": crosswalk_version,
        "attribution": attribution,
        "fetched_at": datetime.now().astimezone().isoformat(),
        "chunks": [
            {
                "name": chunk.name,
                "content_type": chunk.content_type,
                "bytes_raw": len(chunk.payload),
                "record_count": chunk.record_count,
                "request_url": chunk.request_url,
                "fetched_at": chunk.fetched_at.isoformat(),
                **chunk.meta,
            }
            for chunk in chunks
        ],
    }
