"""Distance reader protocol."""

from __future__ import annotations

from typing import Optional, Protocol


class DistanceReader(Protocol):
    """Sensor abstraction used by distance activation logic."""

    @property
    def available(self) -> bool:
        ...

    def read_distance_mm(self) -> Optional[float]:
        ...

    def read_mm(self) -> Optional[float]:
        ...

    def set_timing_budget_ms(self, budget_ms: int) -> bool:
        ...

    def set_intermeasurement_ms(self, intermeasurement_ms: int) -> bool:
        ...
