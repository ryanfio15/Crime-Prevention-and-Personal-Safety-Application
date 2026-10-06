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
from safety.etl.adapters.chicago import ChicagoSocrataAdapter
from safety.etl.adapters.los_angeles import LosAngelesSocrataAdapter
from safety.etl.adapters.philadelphia import PhiladelphiaCartoAdapter
from safety.etl.adapters.seattle import SeattleSocrataAdapter
from safety.etl.adapters.washington_dc import WashingtonDcEsriAdapter

# The registry maps a source_id to its adapter class. Onboarding a seventh city
# is: add a reference.source_registry row, write one adapter, add one line here.
ADAPTERS: dict[str, type[SourceAdapter]] = {
    "phl": PhiladelphiaCartoAdapter,
    "chi": ChicagoSocrataAdapter,
    "sea": SeattleSocrataAdapter,
    "lax": LosAngelesSocrataAdapter,
    "dc": WashingtonDcEsriAdapter,
    # Austin ("aus") has no adapter: neither APD dataset publishes a location
    # finer than a census block group. See docs/PHASE2.md.
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
