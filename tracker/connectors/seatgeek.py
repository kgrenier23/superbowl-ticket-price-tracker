"""SeatGeek connector — Tier A: API (with key), Tier B: network capture from event page."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from tracker.config import settings
from tracker.models import Listing
from tracker.normalize import enrich_section

from .base import BaseConnector, get_playwright_proxy


class SeatGeekConnector(BaseConnector):
    name = "seatgeek"

    _API_BASE = "https://api.seatgeek.com/2"
    _WEB_BASE = "https://seatgeek.com"

    # Network capture patterns for SeatGeek event pages
    _INVENTORY_PATTERNS = [
        "/listings",
        "/api/",
        "seatgeek.com/api",
    ]

    def resolve_event_ref(self) -> str:
        # Prefer explicit event URL
        if settings.seatgeek_event_url:
            return settings.seatgeek_event_url
        if settings.seatgeek_event_id:
            return settings.seatgeek_event_id
        if settings.seatgeek_api_key:
            return self._discover_event_id()
        # Fallback: event URL for scrape
        return f"{self._WEB_BASE}/super-bowl-tickets"

    def _discover_event_id(self) -> str:
        """Search SeatGeek public API for the Super Bowl event."""
        params: dict[str, str] = {"q": f"{settings.event_name} 2026", "per_page": "5"}
        if settings.seatgeek_api_key:
            params["client_id"] = settings.seatgeek_api_key
        resp = httpx.get(
            f"{self._API_BASE}/events",
            params=params,
            headers={"User-Agent": self.USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])
        if not events:
            raise RuntimeError("SeatGeek: could not discover event")
        target = datetime.fromisoformat(settings.event_datetime).replace(
            tzinfo=settings.tz
        )
        best = min(
            events,
            key=lambda e: abs(
                (
                    datetime.fromisoformat(e["datetime_utc"]).replace(
                        tzinfo=timezone.utc
                    )
                    - target
                ).total_seconds()
            ),
        )
        return str(best["id"])

    async def _fetch(self, event_ref: str) -> list[Listing]:
        # Tier A: API if key present and event_ref is numeric ID
        if settings.seatgeek_api_key:
            eid = event_ref
            if not event_ref.isdigit():
                # Extract event ID from URL if possible
                m = re.search(r"/(\d{6,})", event_ref)
                if m:
                    eid = m.group(1)
                else:
                    eid = self._discover_event_id()
            self.logger.info("SeatGeek: using API mode (event_id=%s)", eid)
            return await self._fetch_api(eid)

        # Tier B: scrape from event page via network capture
        self.logger.info("SeatGeek: API key missing; using scrape fallback from event URL")
        url = event_ref if event_ref.startswith("http") else f"{self._WEB_BASE}/e/{event_ref}"
        return await self._fetch_network_capture(url)

    # ------------------------------------------------------------------ Tier A: API

    async def _fetch_api(self, event_id: str) -> list[Listing]:
        params: dict[str, str] = {"client_id": settings.seatgeek_api_key}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{self._API_BASE}/events/{event_id}/listings",
                params=params,
                headers={"User-Agent": self.USER_AGENT},
            )
            resp.raise_for_status()
            data = resp.json()

        now = self._now_utc()
        listings: list[Listing] = []
        for item in data.get("listings", []):
            listing = self._parse_api_item(item, event_id, now)
            if listing:
                listings.append(listing)
        return listings

    def _parse_api_item(self, item: dict, event_id: str, now: datetime) -> Listing | None:
        section = str(item.get("s", item.get("section", "")))
        row = item.get("r", item.get("row"))
        qty = item.get("q", item.get("quantity"))
        price_info = item.get("p", {})
        if isinstance(price_info, dict):
            all_in = price_info.get("a")
            base = price_info.get("l")
        else:
            all_in = None
            base = item.get("price")
        fees = round(all_in - base, 2) if all_in and base else None

        if base is None and all_in is None:
            return None

        pricing_basis = "all_in" if all_in is not None else "base_only"
        display = all_in if all_in is not None else base

        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=event_id,
            section=section,
            row=str(row) if row else None,
            quantity=int(qty) if qty else None,
            base_price=float(base) if base else None,
            fees=float(fees) if fees else None,
            all_in_price=float(all_in) if all_in else None,
            display_price=float(display) if display else None,
            pricing_basis=pricing_basis,
            listing_url=f"https://seatgeek.com/e/{event_id}",
            raw_payload=item,
        )
        enrich_section(listing)
        return listing

    # ------------------------------------------------------------------ Tier B: Network capture

    async def _fetch_network_capture(self, url: str) -> list[Listing]:
        from playwright.async_api import async_playwright

        now = self._now_utc()
        listings: list[Listing] = []

        async with async_playwright() as pw:
            proxy = get_playwright_proxy()
            browser = await pw.chromium.launch(headless=True, proxy=proxy)
            ctx = await browser.new_context(user_agent=self.USER_AGENT)
            page = await ctx.new_page()

            payload = await self.capture_inventory_json(
                page, url,
                url_patterns=self._INVENTORY_PATTERNS,
                timeout_ms=20000,
                min_listings=3,
            )

            if payload:
                listings = self._parse_inventory_payload(payload, url, now)

            # Tier C: embedded state fallback
            if not listings:
                self.logger.info("SeatGeek: Tier B failed, trying embedded state (Tier C)")
                embedded = await self.extract_embedded_json(page)
                if embedded:
                    listings = self._parse_embedded_payload(embedded, url, now)

            if not listings:
                await self._save_debug_artifacts(page, reason="zero_listings")

            await browser.close()
        return listings

    def _parse_inventory_payload(
        self, data: dict | list, url: str, now: datetime
    ) -> list[Listing]:
        """Parse a captured SeatGeek inventory JSON payload."""
        listings: list[Listing] = []

        # Try known SeatGeek structures
        items = []
        if isinstance(data, dict):
            items = data.get("listings", data.get("items", data.get("data", [])))
        elif isinstance(data, list):
            items = data

        if not isinstance(items, list):
            return listings

        for item in items:
            if not isinstance(item, dict):
                continue
            listing = self._parse_captured_item(item, url, now)
            if listing:
                listings.append(listing)
        return listings

    def _parse_captured_item(
        self, item: dict, url: str, now: datetime
    ) -> Listing | None:
        """Parse a single listing item from captured network JSON."""
        # SeatGeek uses compact keys: s=section, r=row, q=qty, p={a=all_in, l=base}
        section = str(item.get("s", item.get("section", item.get("sectionName", ""))))
        row = item.get("r", item.get("row", item.get("rowName")))
        qty = item.get("q", item.get("quantity", item.get("availableQuantity")))

        # Price extraction
        price_info = item.get("p", item.get("price", {}))
        all_in = None
        base = None
        if isinstance(price_info, dict):
            all_in = price_info.get("a", price_info.get("allIn", price_info.get("all_in")))
            base = price_info.get("l", price_info.get("listing", price_info.get("base")))
        elif isinstance(price_info, (int, float)):
            base = price_info

        # Also check top-level price fields
        if base is None:
            base = item.get("price", item.get("listPrice"))
        if all_in is None:
            all_in = item.get("allInPrice", item.get("all_in_price"))

        if base is None and all_in is None:
            return None

        fees = round(float(all_in) - float(base), 2) if all_in and base else None
        pricing_basis = "all_in" if all_in is not None else "base_only"
        display = all_in if all_in is not None else base

        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=url,
            section=section,
            row=str(row) if row else None,
            quantity=int(qty) if qty else None,
            base_price=float(base) if base is not None else None,
            fees=float(fees) if fees is not None else None,
            all_in_price=float(all_in) if all_in is not None else None,
            display_price=float(display) if display is not None else None,
            pricing_basis=pricing_basis,
            listing_url=url,
            raw_payload=item,
        )
        enrich_section(listing)
        return listing

    def _parse_embedded_payload(
        self, data: dict | list, url: str, now: datetime
    ) -> list[Listing]:
        """Parse embedded __NEXT_DATA__ or similar for SeatGeek."""
        listings: list[Listing] = []
        if isinstance(data, dict):
            # Navigate NEXT_DATA structure: props.pageProps.listings
            page_props = data.get("props", {}).get("pageProps", {})
            items = page_props.get("listings", page_props.get("tickets", []))
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        listing = self._parse_captured_item(item, url, now)
                        if listing:
                            listings.append(listing)
        return listings
