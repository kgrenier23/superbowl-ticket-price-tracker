"""On Location Experiences connector — NFL authorized primary/package seller.

This is NOT a standard resale marketplace — it sells hospitality packages.
Tracked separately and excluded from ALL resale rollups.

The ticketing page is on the 49ers subdomain (49ers.onlocationexp.com) and uses
server-rendered HTML with MUI components and data-testid attributes.
"""

from __future__ import annotations

import re
from datetime import datetime

from tracker.config import settings
from tracker.models import Listing
from tracker.normalize import enrich_section

from .base import BaseConnector


class OnLocationConnector(BaseConnector):
    name = "onlocation"

    _WEB_BASE = "https://onlocationexp.com"

    _INVENTORY_PATTERNS = [
        "/api/",
        "/products",
        "/packages",
        "/inventory",
        "onlocationexp.com",
    ]

    def resolve_event_ref(self) -> str:
        if settings.onlocation_event_url:
            return settings.onlocation_event_url
        return f"{self._WEB_BASE}/nfl/super-bowl/tickets"

    async def _fetch(self, event_ref: str) -> list[Listing]:
        from playwright.async_api import async_playwright

        url = event_ref
        now = self._now_utc()
        listings: list[Listing] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=self.USER_AGENT)
            page = await ctx.new_page()

            # Tier B: network capture
            payload = await self.capture_inventory_json(
                page, url,
                url_patterns=self._INVENTORY_PATTERNS,
                timeout_ms=20000,
                min_listings=1,  # OnLocation may have very few packages
            )

            if payload:
                listings = self._parse_payload(payload, url, now)

            # Tier C: embedded state
            if not listings:
                self.logger.info("OnLocation: Tier B failed, trying embedded state (Tier C)")
                embedded = await self.extract_embedded_json(page)
                if embedded:
                    listings = self._parse_payload(embedded, url, now)

            # Tier D: DOM scraping (49ers subdomain uses data-testid attributes)
            if not listings:
                self.logger.info("OnLocation: Tier C failed, trying DOM scraping (Tier D)")
                listings = await self._scrape_dom(page, url, now)

            if not listings:
                await self._save_debug_artifacts(page, reason="zero_listings")

            await browser.close()
        return listings

    async def _scrape_dom(
        self, page, url: str, now: datetime
    ) -> list[Listing]:
        """Scrape listings from data-testid='event-ticket-container' elements."""
        listings: list[Listing] = []

        containers = await page.query_selector_all(
            '[data-testid="event-ticket-container"]'
        )
        self.logger.info(f"OnLocation DOM: found {len(containers)} ticket containers")

        for container in containers:
            try:
                section_el = await container.query_selector(
                    '[data-testid="event-ticket-section"]'
                )
                row_el = await container.query_selector(
                    '[data-testid="event-ticket-row"]'
                )
                price_el = await container.query_selector(
                    '[data-testid="event-ticket-price"]'
                )
                qty_el = await container.query_selector(
                    '[data-testid="event-ticket-quantity"]'
                )

                section = (await section_el.inner_text()).strip() if section_el else ""
                row = (await row_el.inner_text()).strip() if row_el else None
                price_text = (await price_el.inner_text()).strip() if price_el else ""
                qty_text = (await qty_el.inner_text()).strip() if qty_el else ""

                # Parse price: "$7,750.00" -> 7750.0
                price_match = re.search(r"[\d,]+\.?\d*", price_text.replace(",", ""))
                if not price_match:
                    continue
                price = float(price_match.group())

                # Parse quantity: "12 Tickets" -> 12
                qty_match = re.search(r"(\d+)", qty_text)
                qty = int(qty_match.group(1)) if qty_match else None

                listing = self._make_listing(
                    pulled_at_utc=now,
                    event_ref=url,
                    section=section,
                    row=row,
                    quantity=qty,
                    base_price=price,
                    fees=None,
                    all_in_price=None,
                    display_price=price,
                    pricing_basis="base_only",
                    listing_url=url,
                    raw_payload={"section": section, "row": row, "price": price, "quantity": qty},
                )
                enrich_section(listing)
                listings.append(listing)
            except Exception:
                self.logger.debug("OnLocation DOM: failed to parse a container", exc_info=True)
                continue

        return listings

    def _parse_payload(
        self, data: dict | list, url: str, now: datetime
    ) -> list[Listing]:
        listings: list[Listing] = []
        items = []

        if isinstance(data, dict):
            for key in ("packages", "products", "listings", "items"):
                items = data.get(key, [])
                if items:
                    break
            if not items and isinstance(data.get("data"), list):
                items = data["data"]
            if not items and "props" in data:
                pp = data.get("props", {}).get("pageProps", {})
                items = pp.get("packages", pp.get("products", []))
        elif isinstance(data, list):
            items = data

        if not isinstance(items, list):
            return listings

        for item in items:
            if not isinstance(item, dict):
                continue
            listing = self._parse_item(item, url, now)
            if listing:
                listings.append(listing)
        return listings

    def _parse_item(self, item: dict, url: str, now: datetime) -> Listing | None:
        section = str(
            item.get("section", "")
            or item.get("sectionName", "")
            or item.get("name", "")
            or item.get("title", "")
        )
        row = item.get("row", item.get("rowName"))
        qty = item.get("quantity", item.get("availableQuantity"))

        # OnLocation pricing — packages, so price is typically "starting at"
        price = None
        for key in (
            "price", "startingPrice", "starting_at", "listingPrice",
            "basePrice", "packagePrice", "amount",
        ):
            val = item.get(key)
            if isinstance(val, (int, float)):
                price = val
                break
            elif isinstance(val, dict):
                price = val.get("amount", val.get("value", val.get("min")))
                if price is not None:
                    break

        if price is None:
            return None

        # OnLocation packages are base_only (starting at, hospitality package)
        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=url,
            section=section,
            row=str(row) if row else None,
            quantity=int(qty) if qty else None,
            base_price=float(price),
            fees=None,
            all_in_price=None,
            display_price=float(price),
            pricing_basis="base_only",
            listing_url=url,
            raw_payload=item,
        )
        enrich_section(listing)
        return listing
