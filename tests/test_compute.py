"""Tests for tracker.compute."""

from datetime import datetime, timezone

from tracker.compute import (
    check_anomalies,
    compute_combined_listing_weighted,
    compute_combined_market_neutral,
    compute_metrics,
)
from tracker.models import HourlyMetrics, Listing


def _make_listing(
    section: str, all_in: float, base: float | None = None,
    marketplace: str = "test", pricing_basis: str = "all_in",
) -> Listing:
    return Listing(
        marketplace=marketplace,
        pulled_at_utc=datetime.now(timezone.utc),
        event_ref="e1",
        section=section,
        all_in_price=all_in,
        base_price=base or all_in,
        pricing_basis=pricing_basis,
    )


class TestComputeMetrics:
    def test_basic(self):
        listings = [
            _make_listing("101", 5000),
            _make_listing("201", 3000),
            _make_listing("134", 7000),
            _make_listing("305", 2500),
        ]
        m = compute_metrics(listings, "test", datetime.now(timezone.utc))
        assert m.get_in == 2500
        assert m.listing_count == 4
        assert m.mean == 4375.0
        assert m.median == 4000.0
        assert m.loge_best == 5000
        assert m.loge_count == 2

    def test_empty(self):
        m = compute_metrics([], "test", datetime.now(timezone.utc))
        assert m.get_in is None
        assert m.listing_count == 0
        assert m.loge_best is None

    def test_no_loge_sections(self):
        listings = [_make_listing("201", 3000), _make_listing("305", 4000)]
        m = compute_metrics(listings, "test", datetime.now(timezone.utc))
        assert m.loge_best is None
        assert m.loge_count == 0

    def test_base_only_pricing_noted(self):
        listing = Listing(
            marketplace="test",
            pulled_at_utc=datetime.now(timezone.utc),
            event_ref="e1",
            section="101",
            base_price=5000,
            all_in_price=None,
            pricing_basis="base_only",
        )
        m = compute_metrics([listing], "test", datetime.now(timezone.utc))
        assert m.pricing_basis == "base_only"
        assert "base_only" in m.notes
        assert m.get_in == 5000

    def test_mixed_pricing(self):
        l1 = _make_listing("101", 5000, pricing_basis="all_in")
        l2 = _make_listing("201", 3000, pricing_basis="base_only")
        m = compute_metrics([l1, l2], "test", datetime.now(timezone.utc))
        assert m.pricing_basis == "mixed"


class TestCombinedListingWeighted:
    def test_excludes_onlocation(self):
        per_mp = {
            "seatgeek": [_make_listing("101", 5000, marketplace="seatgeek")],
            "onlocation": [_make_listing("110", 12000, marketplace="onlocation")],
        }
        m = compute_combined_listing_weighted(per_mp, datetime.now(timezone.utc))
        assert m.marketplace == "ALL_LISTING_WEIGHTED"
        assert m.listing_count == 1
        assert m.get_in == 5000

    def test_combined(self):
        per_mp = {
            "a": [_make_listing("101", 5000)],
            "b": [_make_listing("201", 3000), _make_listing("134", 7000)],
        }
        m = compute_combined_listing_weighted(per_mp, datetime.now(timezone.utc))
        assert m.get_in == 3000
        assert m.listing_count == 3


class TestCombinedMarketNeutral:
    def test_equal_weight(self):
        now = datetime.now(timezone.utc)
        metrics_a = HourlyMetrics(
            timestamp_utc=now, marketplace="a", event_ref="e1",
            listing_count=100, get_in=3000, mean=5000.0, median=4500.0,
            loge_best=4000, loge_count=10,
        )
        metrics_b = HourlyMetrics(
            timestamp_utc=now, marketplace="b", event_ref="e1",
            listing_count=50, get_in=4000, mean=7000.0, median=6500.0,
            loge_best=5000, loge_count=5,
        )
        m = compute_combined_market_neutral(
            {"a": metrics_a, "b": metrics_b}, now
        )
        assert m.marketplace == "ALL_MARKET_NEUTRAL"
        assert m.get_in == 3000  # min of get-ins
        assert m.mean == 6000.0  # mean of 5000, 7000
        assert m.median == 5500.0  # median of 4500, 6500
        assert m.loge_best == 4000  # min of loge_bests


class TestAnomalies:
    def test_large_move(self):
        now = datetime.now(timezone.utc)
        current = HourlyMetrics(
            timestamp_utc=now, marketplace="test", event_ref="e1",
            listing_count=100, get_in=5000,
        )
        previous = HourlyMetrics(
            timestamp_utc=now, marketplace="test", event_ref="e1",
            listing_count=100, get_in=3000,
        )
        flags = check_anomalies(current, previous)
        assert len(flags) == 1
        assert "LARGE MOVE" in flags[0]

    def test_listing_drop(self):
        now = datetime.now(timezone.utc)
        current = HourlyMetrics(
            timestamp_utc=now, marketplace="test", event_ref="e1",
            listing_count=2, get_in=5000,
        )
        previous = HourlyMetrics(
            timestamp_utc=now, marketplace="test", event_ref="e1",
            listing_count=100, get_in=5000,
        )
        flags = check_anomalies(current, previous)
        assert any("LISTING DROP" in f for f in flags)

    def test_no_anomaly(self):
        now = datetime.now(timezone.utc)
        current = HourlyMetrics(
            timestamp_utc=now, marketplace="test", event_ref="e1",
            listing_count=100, get_in=5000,
        )
        previous = HourlyMetrics(
            timestamp_utc=now, marketplace="test", event_ref="e1",
            listing_count=95, get_in=5100,
        )
        flags = check_anomalies(current, previous)
        assert flags == []
