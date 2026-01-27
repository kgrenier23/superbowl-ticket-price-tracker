"""Compute hourly metrics from a list of Listing objects."""

from __future__ import annotations

import statistics
from datetime import datetime

from tracker.models import HourlyMetrics, Listing
from tracker.normalize import is_lower_level


def compute_metrics(
    listings: list[Listing],
    marketplace: str,
    timestamp_utc: datetime,
    event_ref: str = "",
) -> HourlyMetrics:
    """Compute get-in, mean, median, and loge-best from listings."""
    prices = [l.effective_price for l in listings if l.effective_price is not None]
    notes_parts: list[str] = []

    # Determine pricing basis
    has_all_in = any(l.all_in_price is not None for l in listings)
    has_base_only = any(l.pricing_basis == "base_only" for l in listings)
    if not prices:
        pricing_basis = "all_in"
    elif has_all_in and has_base_only:
        pricing_basis = "mixed"
        notes_parts.append("mixed_pricing")
    elif has_base_only:
        pricing_basis = "base_only"
        notes_parts.append("base_only_pricing")
    else:
        pricing_basis = "all_in"

    get_in = min(prices) if prices else None
    mean = round(statistics.mean(prices), 2) if prices else None
    median = round(statistics.median(prices), 2) if prices else None

    # Lower-level (section 100-199)
    loge_listings = [
        l for l in listings
        if l.effective_price is not None and is_lower_level(l.section)
    ]
    loge_prices = [l.effective_price for l in loge_listings if l.effective_price is not None]
    loge_best = min(loge_prices) if loge_prices else None

    return HourlyMetrics(
        timestamp_utc=timestamp_utc,
        marketplace=marketplace,
        event_ref=event_ref,
        listing_count=len(prices),
        get_in=get_in,
        mean=mean,
        median=median,
        loge_best=loge_best,
        loge_count=len(loge_prices),
        notes="; ".join(notes_parts),
        pricing_basis=pricing_basis,
    )


def compute_combined_listing_weighted(
    per_marketplace: dict[str, list[Listing]],
    timestamp_utc: datetime,
    event_ref: str = "",
) -> HourlyMetrics:
    """ALL_LISTING_WEIGHTED: merge all resale listings, compute aggregate metrics.

    Excludes onlocation (hospitality packages, not resale).
    """
    all_listings = [
        l for name, listings in per_marketplace.items()
        if name != "onlocation"
        for l in listings
    ]
    return compute_metrics(all_listings, "ALL_LISTING_WEIGHTED", timestamp_utc, event_ref)


def compute_combined_market_neutral(
    per_marketplace_metrics: dict[str, HourlyMetrics],
    timestamp_utc: datetime,
    event_ref: str = "",
) -> HourlyMetrics:
    """ALL_MARKET_NEUTRAL: equal weight per marketplace.

    - get_in = min of per-marketplace get-ins
    - mean = mean of per-marketplace means
    - median = median of per-marketplace medians
    - loge_best = min of per-marketplace loge_bests

    Excludes onlocation.
    """
    resale = {k: v for k, v in per_marketplace_metrics.items() if k != "onlocation"}
    if not resale:
        return HourlyMetrics(
            timestamp_utc=timestamp_utc,
            marketplace="ALL_MARKET_NEUTRAL",
            event_ref=event_ref,
            listing_count=0,
        )

    get_ins = [m.get_in for m in resale.values() if m.get_in is not None]
    means = [m.mean for m in resale.values() if m.mean is not None]
    medians = [m.median for m in resale.values() if m.median is not None]
    loges = [m.loge_best for m in resale.values() if m.loge_best is not None]
    total_listings = sum(m.listing_count for m in resale.values())

    bases = {m.pricing_basis for m in resale.values()}
    if "mixed" in bases or ("base_only" in bases and "all_in" in bases):
        pricing_basis = "mixed"
    elif "base_only" in bases:
        pricing_basis = "base_only"
    else:
        pricing_basis = "all_in"

    return HourlyMetrics(
        timestamp_utc=timestamp_utc,
        marketplace="ALL_MARKET_NEUTRAL",
        event_ref=event_ref,
        listing_count=total_listings,
        get_in=min(get_ins) if get_ins else None,
        mean=round(statistics.mean(means), 2) if means else None,
        median=round(statistics.median(medians), 2) if medians else None,
        loge_best=min(loges) if loges else None,
        loge_count=sum(m.loge_count for m in resale.values()),
        pricing_basis=pricing_basis,
    )


# Backward compat alias
def compute_combined(
    per_marketplace: dict[str, list[Listing]],
    timestamp_utc: datetime,
    event_ref: str = "",
) -> HourlyMetrics:
    """Legacy alias: returns listing-weighted rollup."""
    return compute_combined_listing_weighted(per_marketplace, timestamp_utc, event_ref)


def check_anomalies(
    current: HourlyMetrics,
    previous: HourlyMetrics | None,
) -> list[str]:
    """Check for anomalous changes. Returns list of warning strings."""
    flags: list[str] = []
    if previous is None:
        return flags

    if current.get_in and previous.get_in and previous.get_in > 0:
        pct = abs(current.get_in - previous.get_in) / previous.get_in
        if pct > 0.25:
            direction = "UP" if current.get_in > previous.get_in else "DOWN"
            flags.append(
                f"LARGE MOVE: get-in {direction} {pct:.0%} "
                f"(${previous.get_in:,.0f} -> ${current.get_in:,.0f})"
            )

    if (
        previous.listing_count > 20
        and current.listing_count < 5
        and current.listing_count < previous.listing_count * 0.1
    ):
        flags.append(
            f"LISTING DROP: {previous.listing_count} -> {current.listing_count}"
        )

    return flags
