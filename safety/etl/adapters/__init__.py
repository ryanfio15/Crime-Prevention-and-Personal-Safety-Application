"""Per-source adapters (design doc S8.1).

Everything a city does differently -- API paradigm, pagination, field names,
date semantics, ID formatting -- lives in exactly one adapter module. Shared
pipeline code below the adapter layer is written only against
`NormalizedIncident` and is city-agnostic (S11).
"""

from __future__ import annotations

from safety.etl.adapters.base import (
    NormalizedIncident,
    RawChunk,
    SourceAdapter,
    SourceConfig,
)
from safety.etl.adapters.philadelphia import PhiladelphiaCartoAdapter

# The registry maps a source_id to its adapter class. Onboarding a seventh city
# is: add a reference.source_registry row, write one adapter, add one line here.
ADAPTERS: dict[str, type[SourceAdapter]] = {
    "phl": PhiladelphiaCartoAdapter,
}


def get_adapter(config: SourceConfig) -> SourceAdapter:
    try:
        adapter_cls = ADAPTERS[config.source_id]
    except KeyError:
        raise NotImplementedError(
            f"No adapter implemented for source '{config.source_id}'. "
            f"Implemented: {sorted(ADAPTERS)}"
        ) from None
    return adapter_cls(config)


__all__ = [
    "ADAPTERS",
    "NormalizedIncident",
    "RawChunk",
    "SourceAdapter",
    "SourceConfig",
    "get_adapter",
]
