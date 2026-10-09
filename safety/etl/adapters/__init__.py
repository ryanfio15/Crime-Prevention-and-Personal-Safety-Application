"""Per-source adapters (design doc S8.1).

Everything a city does differently -- API paradigm, pagination, field names,
date semantics, ID formatting -- lives in exactly one adapter module. Shared
pipeline code below the adapter layer is written only against
`NormalizedIncident` and is city-agnostic (S11).
"""

from __future__ import annotations

from datetime import date

from safety.etl.adapters.base import (
    NormalizedIncident,
    RawChunk,
    SourceAdapter,
    SourceConfig,
)
from safety.etl.adapters.austin import AustinEsriAdapter
from safety.etl.adapters.chicago import ChicagoSocrataAdapter
from safety.etl.adapters.los_angeles import (
    HISTORY_DATASETS as _LAX_HISTORY_DATASETS,
    NIBRS_HISTORY_START as _LAX_NIBRS_HISTORY_START,
    LosAngelesLegacyAdapter,
    LosAngelesSocrataAdapter,
)
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
    "aus": AustinEsriAdapter,
}

# Older datasets a city publishes in another shape, read only by the history
# load (safety.etl.run history). The history load swaps the dataset into the
# config it passes down, and get_adapter picks the reader by that dataset.
DATASET_ADAPTERS: dict[tuple[str, str], type[SourceAdapter]] = {
    ("lax", dataset): LosAngelesLegacyAdapter for dataset, _, _ in _LAX_HISTORY_DATASETS
}

# Per city: (dataset, first day, day after the last) for each older dataset,
# newest first.
HISTORY_DATASETS: dict[str, tuple[tuple[str, date, date], ...]] = {
    "lax": _LAX_HISTORY_DATASETS,
}

# Where the history load stops reading the city's current dataset, when that
# is later than the city's history_start_date because older years come from
# HISTORY_DATASETS instead.
PRIMARY_HISTORY_START: dict[str, date] = {
    "lax": _LAX_NIBRS_HISTORY_START,
}


def get_adapter(config: SourceConfig) -> SourceAdapter:
    legacy = DATASET_ADAPTERS.get((config.source_id, config.incident_dataset))
    if legacy is not None:
        return legacy(config)
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
    "DATASET_ADAPTERS",
    "HISTORY_DATASETS",
    "PRIMARY_HISTORY_START",
    "NormalizedIncident",
    "RawChunk",
    "SourceAdapter",
    "SourceConfig",
    "get_adapter",
]
