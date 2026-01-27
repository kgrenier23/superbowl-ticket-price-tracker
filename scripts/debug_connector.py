#!/usr/bin/env python3
"""Debug a single connector in isolation — writes debug artifacts to ./debug/.

Usage:
    python scripts/debug_connector.py --marketplace stubhub --url <event_url>
    python scripts/debug_connector.py --marketplace seatgeek
    python scripts/debug_connector.py --marketplace vividseats --url <event_url>

Set DEBUG_CAPTURE=1 to save screenshots, HTML, and network logs.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force debug capture on
os.environ["DEBUG_CAPTURE"] = "1"

from tracker.connectors import ALL_CONNECTORS
from tracker.normalize import enrich_section


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def get_connector(name: str):
    for cls in ALL_CONNECTORS:
        if cls.name == name:
            return cls()
    available = [cls.name for cls in ALL_CONNECTORS]
    print(f"Unknown marketplace: {name!r}. Available: {available}")
    sys.exit(1)


async def run_debug(marketplace: str, url: str | None) -> None:
    connector = get_connector(marketplace)

    if url:
        event_ref = url
    else:
        try:
            event_ref = connector.resolve_event_ref()
        except Exception as exc:
            print(f"Could not resolve event ref: {exc}")
            sys.exit(1)

    print(f"\n=== Debug: {marketplace} ===")
    print(f"Event ref: {event_ref}")
    print(f"Debug artifacts: ./debug/{marketplace}/")
    print()

    try:
        listings = await connector.fetch_listings(event_ref)
    except Exception as exc:
        print(f"FAILED: {exc}")
        import traceback
        traceback.print_exc()
        return

    # Enrich sections
    for l in listings:
        enrich_section(l)

    print(f"\nResults: {len(listings)} listings")
    if listings:
        prices = [l.effective_price for l in listings if l.effective_price]
        if prices:
            print(f"  Price range: ${min(prices):,.0f} - ${max(prices):,.0f}")
            print(f"  Median: ${sorted(prices)[len(prices)//2]:,.0f}")

        lower = [l for l in listings if l.level_bucket == "lower_100s"]
        print(f"  Lower-level (100s): {len(lower)} listings")
        if lower:
            lower_prices = [l.effective_price for l in lower if l.effective_price]
            if lower_prices:
                print(f"  Lower-level range: ${min(lower_prices):,.0f} - ${max(lower_prices):,.0f}")

        print(f"\n  Sample listings:")
        for l in listings[:5]:
            print(
                f"    Sec {l.section_clean or l.section:>6} "
                f"Row {str(l.row or '?'):>4} "
                f"Qty {str(l.quantity or '?'):>2} "
                f"${l.effective_price:>10,.0f} "
                f"({l.pricing_basis}) "
                f"[{l.level_bucket}]"
            )

        # Pricing basis summary
        bases = {l.pricing_basis for l in listings}
        print(f"\n  Pricing bases: {bases}")
    else:
        print("  No listings found. Check debug artifacts for details.")
        print(f"  Debug dir: ./debug/{marketplace}/")


def main():
    parser = argparse.ArgumentParser(description="Debug a single ticket marketplace connector")
    parser.add_argument("--marketplace", "-m", required=True, help="Marketplace name")
    parser.add_argument("--url", "-u", help="Event URL (overrides config)")
    args = parser.parse_args()

    setup_logging()
    asyncio.run(run_debug(args.marketplace, args.url))


if __name__ == "__main__":
    main()
