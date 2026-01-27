"""Generate trend chart PNG from stored metrics — multi-marketplace, two panels."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from tracker.config import settings
from tracker.storage import get_metrics_history

# Colors per marketplace
_COLORS = {
    "seatgeek": "#1DB954",
    "stubhub": "#3B1C8C",
    "vividseats": "#E63946",
    "tickpick": "#457B9D",
    "onlocation": "#F4A261",
    "ALL_LISTING_WEIGHTED": "#222222",
    "ALL_MARKET_NEUTRAL": "#888888",
}
_DEFAULT_COLOR = "#999999"

# Marketplaces to plot (exclude ALL rollups from per-marketplace lines)
_RESALE_MARKETS = {"seatgeek", "stubhub", "vividseats", "tickpick"}


def _event_dt_utc() -> datetime:
    from zoneinfo import ZoneInfo

    local = datetime.fromisoformat(settings.event_datetime).replace(
        tzinfo=ZoneInfo(settings.event_timezone)
    )
    return local.astimezone(timezone.utc)


def _days_until(ts: datetime) -> float:
    event = _event_dt_utc()
    delta = event - ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else event - ts
    return delta.total_seconds() / 86400


def generate_chart(out_path: Path | None = None) -> Path:
    """Generate two-panel trend chart: Get-In prices + Lower-Level Best."""
    now = datetime.now(timezone.utc)
    if out_path is None:
        out_path = settings.out_dir / f"superbowl_prices_trend_{now:%Y%m%d_%H%M}_UTC.png"

    rows = get_metrics_history(days=14)

    # Group by marketplace
    by_market: dict[str, list] = {}
    for r in rows:
        by_market.setdefault(r.marketplace, []).append(r)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
    fig.patch.set_facecolor("white")

    # --- Panel 1: Get-In Price lines ---
    ax1.set_title("Super Bowl Get-In Price by Marketplace", fontsize=14, fontweight="bold", pad=10)
    for market in sorted(by_market.keys()):
        if market not in _RESALE_MARKETS and market != "ALL_LISTING_WEIGHTED":
            continue
        market_rows = by_market[market]
        days = [_days_until(r.timestamp) for r in market_rows]
        vals = [r.get_in for r in market_rows]
        color = _COLORS.get(market, _DEFAULT_COLOR)
        lw = 2.0 if market == "ALL_LISTING_WEIGHTED" else 1.3
        ls = "--" if market == "ALL_LISTING_WEIGHTED" else "-"
        label = market.replace("_", " ").title()
        ax1.plot(days, vals, marker="o", markersize=2, linewidth=lw, linestyle=ls,
                 label=label, color=color)

    ax1.set_ylabel("Get-In Price (USD)", fontsize=11)
    ax1.legend(loc="upper left", fontsize=8, ncol=2)
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))

    # --- Panel 2: Lower-Level Best lines ---
    ax2.set_title("Lower-Level (100s) Best Price by Marketplace", fontsize=14, fontweight="bold", pad=10)
    for market in sorted(by_market.keys()):
        if market not in _RESALE_MARKETS and market != "ALL_LISTING_WEIGHTED":
            continue
        market_rows = by_market[market]
        days = [_days_until(r.timestamp) for r in market_rows]
        vals = [r.loge_best for r in market_rows]
        # Skip if all None
        if all(v is None for v in vals):
            continue
        color = _COLORS.get(market, _DEFAULT_COLOR)
        lw = 2.0 if market == "ALL_LISTING_WEIGHTED" else 1.3
        ls = "--" if market == "ALL_LISTING_WEIGHTED" else "-"
        label = market.replace("_", " ").title()
        ax2.plot(days, vals, marker="^", markersize=2, linewidth=lw, linestyle=ls,
                 label=label, color=color)

    ax2.set_xlabel("Days Until Event", fontsize=11)
    ax2.set_ylabel("Lower-Level Best (USD)", fontsize=11)
    ax2.invert_xaxis()  # countdown: left=14, right=0
    ax2.legend(loc="upper left", fontsize=8, ncol=2)
    ax2.grid(True, alpha=0.3)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))

    # Also invert panel 1 x-axis
    ax1.invert_xaxis()

    fig.text(
        0.99, 0.01,
        f"Last updated: {now:%Y-%m-%d %H:%M} UTC",
        ha="right", va="bottom", fontsize=8, color="gray",
    )

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path
