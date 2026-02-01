"""Vivid Seats connector — Tier B: network capture from event page."""

from __future__ import annotations

from datetime import datetime

from tracker.config import settings
from tracker.models import Listing
from tracker.normalize import enrich_section

from .base import BaseConnector, get_playwright_proxy


class VividSeatsConnector(BaseConnector):
    name = "vividseats"

    _WEB_BASE = "https://www.vividseats.com"

    _INVENTORY_PATTERNS = [
        "/listings",
        "/inventory",
        "/tickets",
        "vividseats.com/hermes/api",
        "vividseats.com/rest/v1",
        "/productions/",
    ]

    def resolve_event_ref(self) -> str:
        if settings.vividseats_event_url:
            return settings.vividseats_event_url
        return f"{self._WEB_BASE}/search?searchTerm=Super+Bowl+2026"

    async def _fetch(self, event_ref: str) -> list[Listing]:
        from playwright.async_api import async_playwright

        url = event_ref
        now = self._now_utc()
        listings: list[Listing] = []

        async with async_playwright() as pw:
            proxy = get_playwright_proxy()
            browser = await pw.chromium.launch(headless=True, proxy=proxy)
            ctx = await browser.new_context(user_agent=self.USER_AGENT)
            page = await ctx.new_page()

            # Tier B: network capture
            payload = await self.capture_inventory_json(
                page, url,
                url_patterns=self._INVENTORY_PATTERNS,
                timeout_ms=20000,
                min_listings=3,
            )

            if payload:
                listings = self._parse_payload(payload, url, now)

            # Tier C: embedded state
            if not listings:
                self.logger.info("VividSeats: Tier B failed, trying embedded state (Tier C)")
                embedded = await self.extract_embedded_json(page)
                if embedded:
                    listings = self._parse_payload(embedded, url, now)

            if not listings:
                await self._save_debug_artifacts(page, reason="zero_listings")

            await browser.close()
        return listings

    def _parse_payload(
        self, data: dict | list, url: str, now: datetime
    ) -> list[Listing]:
        listings: list[Listing] = []
        items = []

        if isinstance(data, dict):
            for key in ("tickets", "listings", "items"):
                items = data.get(key, [])
                if items:
                    break
            if not items and isinstance(data.get("data"), list):
                items = data["data"]
            if not items and "props" in data:
                pp = data.get("props", {}).get("pageProps", {})
                items = pp.get("listings", pp.get("tickets", []))
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
        # VividSeats uses short keys: s=section, r=row, q=quantity, p=base price
        # and also sectionName, row, quantity as longer aliases
        section = str(
            item.get("sectionName", "")
            or item.get("section", "")
            or item.get("s", "")
            or item.get("l", "")  # "l" = label in VividSeats
        )
        row = item.get("row", item.get("rowName", item.get("r")))
        qty = item.get("quantity", item.get("availableQuantity", item.get("q")))
        if isinstance(qty, str):
            try:
                qty = int(qty)
            except ValueError:
                qty = None
        if isinstance(qty, list) and qty:
            qty = qty[0]

        # VividSeats: "p" = base price, "aip"/"allInPricePerTicket" = all-in price
        base = None
        all_in = None

        for key in ("p", "price", "listingPrice", "currentPrice", "basePrice"):
            val = item.get(key)
            if val is not None:
                try:
                    base = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        for key in ("aip", "allInPricePerTicket", "allInPrice", "priceWithFees", "totalPrice"):
            val = item.get(key)
            if val is not None:
                try:
                    all_in = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        if base is None and all_in is None:
            return None

        if all_in is None:
            all_in = base
        if base is None:
            base = all_in

        fees = round(float(all_in) - float(base), 2) if all_in and base and all_in != base else None
        pricing_basis = "all_in" if (all_in and base and all_in != base) else "base_only"

        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=url,
            section=section,
            row=str(row) if row else None,
            quantity=int(qty) if qty else None,
            base_price=float(base) if base is not None else None,
            fees=fees,
            all_in_price=float(all_in) if all_in is not None else None,
            display_price=float(all_in or base),
            pricing_basis=pricing_basis,
            listing_url=url,
            raw_payload=item,
        )
        enrich_section(listing)
        return listing
