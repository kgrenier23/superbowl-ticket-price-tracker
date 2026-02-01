"""StubHub connector — Tier A: OAuth2 API, Tier B: network capture from event page."""

from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from tracker.config import settings
from tracker.models import Listing
from tracker.normalize import enrich_section

from .base import BaseConnector, get_playwright_proxy


class StubHubConnector(BaseConnector):
    name = "stubhub"

    _TOKEN_URL = "https://account.stubhub.com/oauth2/token"
    _API_BASE = "https://api.stubhub.net"
    _WEB_BASE = "https://www.stubhub.com"

    _access_token: str | None = None
    _token_expires_at: float = 0.0

    # Network capture patterns for StubHub event pages
    _INVENTORY_PATTERNS = [
        "GetMapAvailabilityAndPrices",
        "/listings",
        "/inventory",
        "stubhub.com/catalog",
        "stubhub.com/search/inventory",
        "stubhub.com/sellers",
        "/gridV2",
    ]

    # DOM selectors for listing cards (Tier D fallback)
    _CARD_SELECTORS = [
        "div[data-listing-id]",           # StubHub primary: data attributes
        "[data-testid='listing-card']",
        "[class*='ListingCard']",
        "[class*='TicketCard']",
        "div[id^='listing-']",
    ]

    # ------------------------------------------------------------------
    # API mode detection
    # ------------------------------------------------------------------

    @property
    def _api_enabled(self) -> bool:
        return bool(settings.stubhub_client_id and settings.stubhub_client_secret)

    # ------------------------------------------------------------------
    # OAuth2 token
    # ------------------------------------------------------------------

    async def _get_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        encoded_id = quote(settings.stubhub_client_id, safe="")
        encoded_secret = quote(settings.stubhub_client_secret, safe="")
        raw = f"{encoded_id}:{encoded_secret}"
        b64 = base64.b64encode(raw.encode()).decode()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self._TOKEN_URL,
                headers={
                    "Authorization": f"Basic {b64}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": self.USER_AGENT,
                },
                data={"grant_type": "client_credentials", "scope": "read:events"},
            )
            resp.raise_for_status()
            body = resp.json()

        self._access_token = body["access_token"]
        self._token_expires_at = time.time() + body.get("expires_in", 86400)
        return self._access_token

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
        }

    # ------------------------------------------------------------------
    # Event resolution
    # ------------------------------------------------------------------

    def resolve_event_ref(self) -> str:
        if settings.stubhub_event_url:
            return settings.stubhub_event_url
        if settings.stubhub_event_id:
            return settings.stubhub_event_id
        if self._api_enabled:
            return "__discover__"
        return f"{self._WEB_BASE}/find/s/?q=Super+Bowl+2026"

    async def _discover_event_id_api(self) -> str:
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{self._API_BASE}/catalog/events",
                params={"page": "1", "page_size": "10"},
                headers=self._auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()

        events = data.get("_embedded", {}).get("items", data.get("items", []))
        if not events:
            raise RuntimeError("StubHub API: no events found")

        from zoneinfo import ZoneInfo
        target = datetime.fromisoformat(settings.event_datetime).replace(
            tzinfo=ZoneInfo(settings.event_timezone)
        )
        best = None
        best_delta = float("inf")
        for ev in events:
            start = ev.get("start_date")
            if not start:
                continue
            ev_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            delta = abs((ev_dt - target).total_seconds())
            if delta < best_delta:
                best_delta = delta
                best = ev
        if best is None:
            raise RuntimeError("StubHub API: could not match event")
        eid = str(best["id"])
        self.logger.info("StubHub API: discovered event id=%s", eid)
        return eid

    # ------------------------------------------------------------------
    # Fetch dispatch
    # ------------------------------------------------------------------

    async def _fetch(self, event_ref: str) -> list[Listing]:
        if self._api_enabled:
            if event_ref == "__discover__":
                event_ref = await self._discover_event_id_api()
            if event_ref.isdigit():
                return await self._fetch_api(event_ref)

        # Tier B: network capture from event page
        url = event_ref if event_ref.startswith("http") else f"{self._WEB_BASE}/event/{event_ref}/"
        self.logger.info("StubHub: using network capture from %s", url)
        return await self._fetch_network_capture(url)

    # ------------------------------------------------------------------
    # Tier A: API
    # ------------------------------------------------------------------

    async def _fetch_api(self, event_id: str) -> list[Listing]:
        token = await self._get_token()
        now = self._now_utc()
        listings: list[Listing] = []
        page = 1
        max_pages = 5

        while page <= max_pages:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{self._API_BASE}/catalog/events/{event_id}/listings",
                    params={"page": str(page), "page_size": "100"},
                    headers=self._auth_headers(token),
                )

            if resp.status_code == 404:
                return await self._fetch_api_seller_listings(event_id, token, now)

            resp.raise_for_status()
            data = resp.json()
            items = data.get("_embedded", {}).get("items", data.get("items", []))
            if not items:
                break

            for item in items:
                listing = self._parse_api_listing(item, event_id, now)
                if listing:
                    listings.append(listing)

            total = data.get("total_items", 0)
            if page * 100 >= total:
                break
            page += 1

        return listings

    async def _fetch_api_seller_listings(
        self, event_id: str, token: str, now: datetime
    ) -> list[Listing]:
        listings: list[Listing] = []
        page = 1
        while page <= 5:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{self._API_BASE}/v2/sellerlistings",
                    params={"event_id": event_id, "page": str(page), "page_size": "100"},
                    headers=self._auth_headers(token),
                )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("_embedded", {}).get("items", data.get("items", []))
            if not items:
                break
            for item in items:
                listing = self._parse_seller_listing(item, event_id, now)
                if listing:
                    listings.append(listing)
            total = data.get("total_items", 0)
            if page * 100 >= total:
                break
            page += 1
        return listings

    def _parse_api_listing(self, item: dict, event_id: str, now: datetime) -> Listing | None:
        section = str(item.get("section", item.get("seating", {}).get("section", "")))
        row = item.get("row", item.get("seating", {}).get("row"))
        qty = item.get("quantity", item.get("number_of_tickets"))

        ticket_price = item.get("ticket_price", {})
        all_in_price_obj = item.get("all_in_price", item.get("estimated_total_ticket_price", {}))

        base = ticket_price.get("amount") if isinstance(ticket_price, dict) else None
        all_in = all_in_price_obj.get("amount") if isinstance(all_in_price_obj, dict) else None

        if base is None:
            base = item.get("price", {}).get("amount") if isinstance(item.get("price"), dict) else item.get("price")
        if all_in is None:
            all_in = base

        if base is None and all_in is None:
            return None

        fees = round(float(all_in) - float(base), 2) if all_in and base else None
        pricing_basis = "all_in" if (all_in is not None and all_in != base) else "base_only"
        display = all_in if all_in is not None else base

        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=event_id,
            section=section,
            row=str(row) if row else None,
            quantity=int(qty) if qty else None,
            base_price=float(base) if base is not None else None,
            fees=fees,
            all_in_price=float(all_in) if all_in is not None else None,
            display_price=float(display) if display is not None else None,
            pricing_basis=pricing_basis,
            listing_url=f"{self._WEB_BASE}/event/{event_id}/",
            raw_payload=item,
        )
        enrich_section(listing)
        return listing

    def _parse_seller_listing(self, item: dict, event_id: str, now: datetime) -> Listing | None:
        seating = item.get("seating", {})
        section = str(seating.get("section", ""))
        row = seating.get("row")
        qty = item.get("number_of_tickets")
        ticket_price = item.get("ticket_price", {})
        base = ticket_price.get("amount") if isinstance(ticket_price, dict) else None
        if base is None:
            return None

        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=event_id,
            section=section,
            row=str(row) if row else None,
            quantity=int(qty) if qty else None,
            base_price=float(base),
            fees=None,
            all_in_price=float(base),
            display_price=float(base),
            pricing_basis="base_only",
            listing_url=f"{self._WEB_BASE}/event/{event_id}/",
            raw_payload=item,
        )
        enrich_section(listing)
        return listing

    # ------------------------------------------------------------------
    # Tier B: Network capture
    # ------------------------------------------------------------------

    async def _fetch_network_capture(self, url: str) -> list[Listing]:
        from playwright.async_api import async_playwright

        now = self._now_utc()
        listings: list[Listing] = []

        async with async_playwright() as pw:
            proxy = get_playwright_proxy()
            browser = await pw.chromium.launch(headless=True, proxy=proxy)
            ctx = await browser.new_context(user_agent=self.USER_AGENT)
            page = await ctx.new_page()

            # Tier B: directly capture GetMapAvailabilityAndPrices JSON
            map_payload = None
            generic_payload = None

            async def _on_response(response):
                nonlocal map_payload, generic_payload
                try:
                    resp_url = response.url
                    ct = response.headers.get("content-type", "")
                    if response.status != 200 or "json" not in ct:
                        return
                    if "GetMapAvailabilityAndPrices" in resp_url:
                        map_payload = await response.json()
                    elif any(p in resp_url for p in self._INVENTORY_PATTERNS):
                        body = await response.json()
                        if isinstance(body, (dict, list)):
                            generic_payload = body
                except Exception:
                    pass

            page.on("response", _on_response)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(8000)

            # Parse map data (sectionPopupData has comprehensive pricing)
            if map_payload:
                listings = await self._parse_map_availability(map_payload, url, now, page=page)

            # Fallback: try generic network payload
            if not listings and generic_payload:
                listings = self._parse_network_payload(generic_payload, url, now)
                if not listings:
                    listings = self._parse_venue_map_payload(generic_payload, url, now)

            # Dismiss "How many tickets?" modal if present
            await self._dismiss_modal(page)

            # Tier C: embedded state
            if not listings:
                self.logger.info("StubHub: Tier B failed, trying embedded state (Tier C)")
                embedded = await self.extract_embedded_json(page)
                if embedded:
                    listings = self._parse_network_payload(embedded, url, now)

            # Tier D: DOM scraping of visible listing cards
            if not listings:
                self.logger.info("StubHub: Tier C failed, trying DOM scrape (Tier D)")
                listings = await self._scrape_listing_cards(page, url, now)

            if not listings:
                await self._save_debug_artifacts(page, reason="zero_listings")

            await browser.close()
        return listings

    async def _parse_map_availability(
        self, data: dict, url: str, now: datetime, page=None,
    ) -> list[Listing]:
        """Parse GetMapAvailabilityAndPrices — section-level min prices.

        Each sectionPopupData entry represents one venue section with its
        cheapest listing price.  We create one Listing per section.
        If *page* is provided, extracts section ID-to-name mapping from the
        inline SVG in the page HTML.
        """
        listings: list[Listing] = []
        spd = data.get("sectionPopupData", {})
        if not isinstance(spd, dict) or not spd:
            return listings

        # Build section-id -> display-name map from inline SVG
        section_name_map: dict[str, str] = {}
        if page:
            try:
                section_name_map = await self._extract_section_name_map(page)
                self.logger.info(
                    "StubHub: extracted %d section name mappings from SVG",
                    len(section_name_map),
                )
            except Exception:
                self.logger.debug("StubHub: SVG section name extraction failed", exc_info=True)

        for key, val in spd.items():
            if not isinstance(val, dict):
                continue
            price = val.get("rawMinPrice")
            if not price:
                continue

            # Key format: ticketClassId_sectionId
            parts = key.split("_")
            section_id = parts[1] if len(parts) > 1 else key
            display_name = section_name_map.get(section_id, section_id)
            row_text = val.get("rowText") or None
            ticket_count = val.get("ticketCount")

            listing = self._make_listing(
                pulled_at_utc=now,
                event_ref=url,
                section=display_name,
                row=row_text,
                quantity=int(ticket_count) if ticket_count else None,
                base_price=None,
                fees=None,
                all_in_price=float(price),
                display_price=float(price),
                pricing_basis="all_in",
                listing_url=url,
                raw_payload=val,
            )
            enrich_section(listing)
            listings.append(listing)

        if listings:
            self.logger.info(
                "StubHub: parsed %d sections from GetMapAvailabilityAndPrices",
                len(listings),
            )
        return listings

    async def _extract_section_name_map(self, page) -> dict[str, str]:
        """Extract section-ID -> display-name mapping from inline SVG.

        StubHub embeds an SVG venue map with <g sprite-identifier="s{id}">
        groups and <text> labels.  We parse the raw HTML to pair each text
        label with the nearest preceding sprite-identifier by string position.
        """
        html = await page.content()
        sprite_positions: list[tuple[int, str]] = []
        text_positions: list[tuple[int, str]] = []

        for m in re.finditer(r'sprite-identifier="s(\d+)"', html):
            sprite_positions.append((m.start(), m.group(1)))

        for m in re.finditer(r"<text[^>]*>([^<]+)</text>", html):
            label = m.group(1).strip()
            if label:
                text_positions.append((m.start(), label))

        if not sprite_positions or not text_positions:
            return {}

        mapping: dict[str, str] = {}
        for text_pos, label in text_positions:
            best_id: str | None = None
            best_dist = float("inf")
            for sprite_pos, sprite_id in sprite_positions:
                dist = abs(text_pos - sprite_pos)
                if dist < best_dist:
                    best_dist = dist
                    best_id = sprite_id
            if best_id and best_dist < 5000:
                mapping[best_id] = label

        return mapping

    async def _dismiss_modal(self, page) -> None:
        """Try to dismiss the 'How many tickets?' modal."""
        try:
            # Look for Continue button in the modal
            for selector in [
                "button:has-text('Continue')",
                "[data-testid='qty-continue']",
                "button[class*='continue']",
                "button[class*='Continue']",
            ]:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    self.logger.info("StubHub: dismissed ticket qty modal")
                    await page.wait_for_timeout(2000)
                    return
        except Exception:
            self.logger.debug("No modal to dismiss", exc_info=True)

    async def _scrape_listing_cards(
        self, page, url: str, now: datetime
    ) -> list[Listing]:
        """Tier D: scrape listing cards from the visible DOM."""
        listings: list[Listing] = []

        # Wait for content to settle after modal dismiss
        await page.wait_for_timeout(2000)

        # Try scrolling to load more listings
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(500)

        cards = []
        for sel in self._CARD_SELECTORS:
            cards = await page.query_selector_all(sel)
            if cards:
                self.logger.info("StubHub DOM: found %d cards with '%s'", len(cards), sel)
                break

        for card in cards:
            # First try data attributes (StubHub puts price in data-price)
            listing = await self._parse_card_attrs(card, url, now)
            if listing:
                listings.append(listing)
                continue
            # Fallback: parse inner text
            text = await card.inner_text()
            listing = self._parse_card_text(text, url, now)
            if listing:
                listings.append(listing)

        return listings

    async def _parse_card_attrs(
        self, card, url: str, now: datetime
    ) -> Listing | None:
        """Parse listing from StubHub data-* attributes on card element."""
        try:
            listing_id = await card.get_attribute("data-listing-id")
            price_str = await card.get_attribute("data-price")
            if not price_str:
                return None

            price = float(price_str.replace("$", "").replace(",", ""))
            text = await card.inner_text()

            section = ""
            row = None
            qty = None
            sec_m = re.search(r"Section\s+(\w+)", text, re.I)
            if sec_m:
                section = sec_m.group(1)
            row_m = re.search(r"Row\s+(\w+)", text, re.I)
            if row_m:
                row = row_m.group(1)
            qty_m = re.search(r"(\d+)\s*ticket", text, re.I)
            if qty_m:
                qty = int(qty_m.group(1))

            # StubHub shows "incl. fees" on all prices
            is_all_in = "incl" in text.lower() or "fees" in text.lower()

            listing = self._make_listing(
                pulled_at_utc=now,
                event_ref=url,
                section=section,
                row=row,
                quantity=qty,
                base_price=None,
                fees=None,
                all_in_price=price if is_all_in else None,
                display_price=price,
                pricing_basis="all_in" if is_all_in else "base_only",
                listing_url=url,
                raw_payload={
                    "listing_id": listing_id,
                    "data_price": price_str,
                    "card_text": text,
                },
            )
            enrich_section(listing)
            return listing
        except Exception:
            return None

    def _parse_card_text(
        self, text: str, url: str, now: datetime
    ) -> Listing | None:
        """Parse a listing card's visible text for section, row, price."""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        section = ""
        row = None
        price: float | None = None
        qty: int | None = None
        is_all_in = False

        for line in lines:
            sec_m = re.search(r"Section\s+(\w+)", line, re.I)
            if sec_m:
                section = sec_m.group(1)
            row_m = re.search(r"Row\s+(\w+)", line, re.I)
            if row_m:
                row = row_m.group(1)
            qty_m = re.search(r"(\d+)\s*ticket", line, re.I)
            if qty_m:
                qty = int(qty_m.group(1))
            price_m = re.findall(r"\$[\d,]+(?:\.\d{2})?", line)
            if price_m:
                price = float(price_m[-1].replace("$", "").replace(",", ""))
            if re.search(r"incl\.?\s*fees|all.in", line, re.I):
                is_all_in = True

        if price is None:
            return None

        listing = self._make_listing(
            pulled_at_utc=now,
            event_ref=url,
            section=section,
            row=row,
            quantity=qty,
            base_price=price if not is_all_in else None,
            fees=None,
            all_in_price=price if is_all_in else None,
            display_price=price,
            pricing_basis="all_in" if is_all_in else "base_only",
            listing_url=url,
            raw_payload={"raw_text": text},
        )
        enrich_section(listing)
        return listing

    def _parse_venue_map_payload(
        self, data: dict | list, url: str, now: datetime
    ) -> list[Listing]:
        """Parse GetMapAvailabilityAndPrices response — section-level pricing."""
        listings: list[Listing] = []
        if not isinstance(data, dict):
            return listings

        # This endpoint returns section-level availability with prices
        # Look for sections array or availability map
        sections = []
        for key in ("sections", "availableSections", "sectionPrices", "categories"):
            val = data.get(key)
            if isinstance(val, list) and val:
                sections = val
                break
        # Also try nested structures
        if not sections:
            for val in data.values():
                if isinstance(val, list) and len(val) > 3:
                    # Heuristic: if list items have price-like keys
                    if val and isinstance(val[0], dict):
                        sample = val[0]
                        if any(k in sample for k in ("price", "minPrice", "sectionId", "sectionName")):
                            sections = val
                            break

        for sec in sections:
            if not isinstance(sec, dict):
                continue
            section = str(
                sec.get("sectionName", "")
                or sec.get("section", "")
                or sec.get("name", "")
                or sec.get("sectionId", "")
            )
            # Price
            price = None
            for key in ("price", "minPrice", "lowestPrice", "fromPrice", "minListPrice"):
                val = sec.get(key)
                if isinstance(val, (int, float)):
                    price = val
                    break
                elif isinstance(val, dict):
                    price = val.get("amount", val.get("value"))
                    if price is not None:
                        break

            if price is None or not section:
                continue

            count = sec.get("listingCount", sec.get("count", sec.get("availableCount", 1)))

            listing = self._make_listing(
                pulled_at_utc=now,
                event_ref=url,
                section=section,
                row=None,
                quantity=int(count) if count else None,
                base_price=float(price),
                fees=None,
                all_in_price=float(price),  # StubHub map shows "incl. fees"
                display_price=float(price),
                pricing_basis="all_in",
                listing_url=url,
                raw_payload=sec,
            )
            enrich_section(listing)
            listings.append(listing)

        if listings:
            self.logger.info("StubHub: parsed %d sections from venue map data", len(listings))
        return listings

    def _parse_network_payload(
        self, data: dict | list, url: str, now: datetime
    ) -> list[Listing]:
        """Parse captured StubHub inventory JSON."""
        listings: list[Listing] = []
        items = []

        if isinstance(data, dict):
            # Try various StubHub response shapes
            for key_path in [
                lambda d: d.get("items", []),
                lambda d: d.get("listings", []),
                lambda d: d.get("_embedded", {}).get("items", []),
                lambda d: d.get("grid", {}).get("items", []),
                lambda d: d.get("data", {}).get("items", []) if isinstance(d.get("data"), dict) else [],
            ]:
                items = key_path(data)
                if items:
                    break
            # Also check NEXT_DATA structure
            if not items and "props" in data:
                page_props = data.get("props", {}).get("pageProps", {})
                items = page_props.get("listings", page_props.get("initialListings", []))
        elif isinstance(data, list):
            items = data

        if not isinstance(items, list):
            return listings

        for item in items:
            if not isinstance(item, dict):
                continue
            listing = self._parse_network_item(item, url, now)
            if listing:
                listings.append(listing)
        return listings

    def _parse_network_item(
        self, item: dict, url: str, now: datetime
    ) -> Listing | None:
        """Parse a single item from StubHub network JSON."""
        # Section: multiple possible keys
        section = (
            item.get("section", "")
            or item.get("sectionName", "")
            or (item.get("seating", {}).get("section", "") if isinstance(item.get("seating"), dict) else "")
        )
        section = str(section)
        row = (
            item.get("row")
            or item.get("rowName")
            or (item.get("seating", {}).get("row") if isinstance(item.get("seating"), dict) else None)
        )
        qty = item.get("quantity", item.get("availableQuantities", item.get("number_of_tickets")))
        if isinstance(qty, list) and qty:
            qty = qty[0]

        # Price: try multiple shapes
        all_in = None
        base = None

        # Shape 1: {price: X, rawPrice: Y}
        if "price" in item and isinstance(item["price"], (int, float)):
            base = item["price"]
        # Shape 2: {price: {amount: X}}
        elif isinstance(item.get("price"), dict):
            base = item["price"].get("amount")

        # All-in variants
        for key in ("allInPrice", "all_in_price", "estimatedFinalPrice", "priceWithFees"):
            val = item.get(key)
            if isinstance(val, (int, float)):
                all_in = val
                break
            elif isinstance(val, dict):
                all_in = val.get("amount")
                if all_in is not None:
                    break

        # Ticket price object
        tp = item.get("ticket_price", item.get("ticketPrice", {}))
        if isinstance(tp, dict) and base is None:
            base = tp.get("amount")
        aip = item.get("all_in_price", item.get("estimated_total_ticket_price", {}))
        if isinstance(aip, dict) and all_in is None:
            all_in = aip.get("amount")

        # Fallback
        if base is None and all_in is None:
            # Check for 'listingPrice' or 'currentPrice'
            for key in ("listingPrice", "currentPrice", "displayPrice"):
                val = item.get(key)
                if isinstance(val, (int, float)):
                    base = val
                    break
                elif isinstance(val, dict):
                    base = val.get("amount")
                    if base is not None:
                        break

        if base is None and all_in is None:
            return None

        if all_in is None:
            all_in = base
        if base is None:
            base = all_in

        fees = round(float(all_in) - float(base), 2) if all_in and base and all_in != base else None
        pricing_basis = "all_in" if (all_in is not None and all_in != base) else "base_only"

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
