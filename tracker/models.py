"""Data models for listings and metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Listing:
    marketplace: str
    pulled_at_utc: datetime
    event_ref: str
    section: str
    row: str | None = None
    quantity: int | None = None
    base_price: float | None = None
    fees: float | None = None
    all_in_price: float | None = None
    currency: str = "USD"
    listing_url: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)

    # --- price clarity ---
    display_price: float | None = None
    pricing_basis: str = "all_in"  # "all_in" or "base_only"

    # --- section enrichment ---
    section_raw: str = ""
    section_clean: str = ""
    section_number: int | None = None
    level_bucket: str = "unknown"  # "lower_100s", "other", "unknown"

    @property
    def effective_price(self) -> float | None:
        """Return all_in_price if available, else base_price."""
        if self.all_in_price is not None:
            return self.all_in_price
        return self.base_price


@dataclass
class HourlyMetrics:
    timestamp_utc: datetime
    marketplace: str
    event_ref: str
    listing_count: int
    get_in: float | None = None
    mean: float | None = None
    median: float | None = None
    loge_best: float | None = None
    loge_count: int = 0
    notes: str = ""
    pricing_basis: str = "all_in"  # "all_in", "base_only", or "mixed"


@dataclass
class ConnectorStatus:
    """Status report for a single connector run."""
    marketplace: str
    ok: bool
    listing_count: int = 0
    pricing_basis: str = "all_in"
    error: str = ""
    last_success: str = ""
    is_sold_out: bool = False
