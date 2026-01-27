"""TickPick connector — Tier B: network capture. TickPick shows all-in prices by default."""

from __future__ import annotations

from datetime import datetime

from tracker.config import settings
from tracker.models import Listing
from tracker.normalize import enrich_section

from .base import BaseConnector


class TickPickConnector(BaseConnector):
    name = "tickpick"

    _WEB_BASE = "https://www.tickpick.com"

    _INVENTORY_PATTERNS = [
        "/listings",
        "/inventory",
        "/tickets",
        "tickpick.com/buy",
        "tickpick.com/api",
        "/event/",
    ]

    def resolve_event_ref(self) -> str:
        if settings.tickpick_event_url:
            return settings.tickpick_event_url
        return f"{self._WEB_BASE}/search?q=Super+Bowl+2026"

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
                min_listings=3,
            )

            if payload:
                listings = self._parse_payload(payload, url, now)

            # Tier C: embedded state
            if not listings:
                self.logger.info("TickPick: Tier B failed, trying embedded state (Tier C)")
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
            for key in ("listings", "items", "tickets"):
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
        # TickPick uses: sid=section, r=row, q=quantity, p=price
        section = str(
            item.get("sid", "")
            or item.get("section", "")
            or item.get("sectionName", "")
            or item.get("s", "")
        )
        row = item.get("row", item.get("rowName", item.get("r")))
        qty = item.get("quantity", item.get("availableQuantity", item.get("q")))
        if isinstance(qty, list) and qty:
            qty = qty[0]

        # Filter out parking passes
        note = str(item.get("n", ""))
        tags = item.get("d", [])
        if isinstance(tags, list) and "pk" in tags:
            return None
        if "parking" in section.lower() or "parking" in note.lower()[:30]:
            return None

        # TickPick prices are all-in by default (no hidden fees)
        price = None
        for key in ("p", "price", "listingPrice", "currentPrice", "ourPrice", "basePrice"):
            val = item.get(key)
            if val is not None:
                try:
                    price = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        if price is None:
            return None

        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=url,
            section=section,
            row=str(row) if row else None,
            quantity=int(qty) if qty else None,
            base_price=float(price),
            fees=0.0,
            all_in_price=float(price),
            display_price=float(price),
            pricing_basis="all_in",  # TickPick is always all-in
            listing_url=url,
            raw_payload=item,
        )
        enrich_section(listing)
        return listing
