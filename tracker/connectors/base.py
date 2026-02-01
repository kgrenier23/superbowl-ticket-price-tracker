"""Abstract base connector interface with debug tooling and network capture helpers."""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tracker.models import Listing

logger = logging.getLogger(__name__)

_DEBUG_CAPTURE = os.environ.get("DEBUG_CAPTURE", "").strip() in ("1", "true", "yes")
_DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "./debug"))


def get_playwright_proxy() -> dict | None:
    """Get proxy configuration for Playwright from environment variables."""
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not proxy_url:
        return None

    try:
        parsed = urlparse(proxy_url)
        proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy_config["username"] = parsed.username
        if parsed.password:
            proxy_config["password"] = parsed.password
        return proxy_config
    except Exception:
        return None


class BaseConnector(abc.ABC):
    """Every marketplace connector inherits from this."""

    name: str = "base"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"connector.{self.name}")

    # -- public entry point --------------------------------------------------

    async def fetch_listings(self, event_ref: str) -> list[Listing]:
        """Fetch listings with retry / back-off. Returns [] on failure."""
        for attempt in range(1, 4):
            try:
                listings = await self._fetch(event_ref)
                self.logger.info(
                    "%s: fetched %d listings (attempt %d)",
                    self.name, len(listings), attempt,
                )
                return listings
            except Exception:
                wait = 2 ** attempt + random.uniform(0, 1)
                self.logger.warning(
                    "%s: attempt %d failed, retrying in %.1fs",
                    self.name, attempt, wait, exc_info=True,
                )
                await asyncio.sleep(wait)
        self.logger.error("%s: all attempts failed", self.name)
        return []

    # -- subclasses implement this -------------------------------------------

    @abc.abstractmethod
    async def _fetch(self, event_ref: str) -> list[Listing]:
        """Fetch and parse listings. Raise on failure."""
        ...

    @abc.abstractmethod
    def resolve_event_ref(self) -> str:
        """Return the event reference (URL or ID) for this connector."""
        ...

    # -- helpers -------------------------------------------------------------

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _make_listing(self, **kwargs: Any) -> Listing:
        kwargs.setdefault("marketplace", self.name)
        kwargs.setdefault("pulled_at_utc", self._now_utc())
        return Listing(**kwargs)

    # -- debug artifacts -----------------------------------------------------

    def _debug_dir(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        d = _DEBUG_DIR / self.name / ts
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _save_debug_artifacts(
        self,
        page,
        network_log: list[dict] | None = None,
        inventory_json: Any = None,
        reason: str = "failure",
    ) -> None:
        """Save debug artifacts (screenshot, HTML, network summary, inventory JSON)."""
        if not _DEBUG_CAPTURE:
            return
        try:
            d = self._debug_dir()
            self.logger.info("Saving debug artifacts to %s (reason: %s)", d, reason)

            try:
                await page.screenshot(path=str(d / "screenshot.png"), full_page=True)
            except Exception:
                self.logger.debug("Could not save screenshot", exc_info=True)

            try:
                html = await page.content()
                (d / "page.html").write_text(html, encoding="utf-8")
            except Exception:
                self.logger.debug("Could not save HTML", exc_info=True)

            if network_log:
                (d / "network_summary.json").write_text(
                    json.dumps(network_log[:500], indent=2, default=str),
                    encoding="utf-8",
                )

            if inventory_json is not None:
                (d / "inventory.json").write_text(
                    json.dumps(inventory_json, indent=2, default=str),
                    encoding="utf-8",
                )
        except Exception:
            self.logger.debug("Failed to save debug artifacts", exc_info=True)

    # -- network capture helper ----------------------------------------------

    async def capture_inventory_json(
        self,
        page,
        url: str,
        url_patterns: list[str] | None = None,
        timeout_ms: int = 15000,
        min_listings: int = 5,
    ) -> dict | list | None:
        """Navigate to URL, capture JSON inventory responses from network.

        Args:
            page: Playwright page object (already in a browser context).
            url: URL to navigate to.
            url_patterns: Optional list of URL substrings to filter responses.
            timeout_ms: Max time to wait for a good payload.
            min_listings: Minimum number of items to consider it inventory.

        Returns:
            The best inventory payload found, or None.
        """
        captured: list[dict] = []
        network_log: list[dict] = []
        best_payload: dict | list | None = None
        best_count = 0

        async def _on_response(response):
            nonlocal best_payload, best_count
            try:
                resp_url = response.url
                content_type = response.headers.get("content-type", "")
                status = response.status
                network_log.append({
                    "url": resp_url[:300],
                    "status": status,
                    "content_type": content_type[:100],
                })

                if status != 200:
                    return
                if "application/json" not in content_type and "text/json" not in content_type:
                    return

                if url_patterns:
                    if not any(pat in resp_url for pat in url_patterns):
                        return

                body = await response.json()
                captured.append({"url": resp_url, "body": body})

                count = _count_listing_items(body)
                if count >= min_listings and count > best_count:
                    best_payload = body
                    best_count = count
                    self.logger.info(
                        "Captured inventory payload: %s (%d items)",
                        resp_url[:120], count,
                    )
            except Exception:
                pass

        page.on("response", _on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(min(5000, timeout_ms // 2))
        except Exception as exc:
            self.logger.warning("Page navigation issue for %s: %s", url, exc)

        if _DEBUG_CAPTURE:
            await self._save_debug_artifacts(
                page,
                network_log=network_log,
                inventory_json=best_payload,
                reason="capture_complete" if best_payload else "no_inventory_found",
            )

        return best_payload

    # -- Tier C: embedded state fallback ------------------------------------

    async def extract_embedded_json(self, page) -> dict | list | None:
        """Try to extract inventory from embedded page state (Tier C fallback)."""
        for js_expr, label in [
            ("JSON.stringify(window.__NEXT_DATA__)", "__NEXT_DATA__"),
            ("JSON.stringify(window.__APOLLO_STATE__)", "__APOLLO_STATE__"),
        ]:
            try:
                raw = await page.evaluate(js_expr)
                if raw and raw != "null" and raw != "undefined":
                    data = json.loads(raw)
                    count = _count_listing_items(data)
                    if count >= 3:
                        self.logger.info(
                            "Found %d items in embedded %s", count, label
                        )
                        return data
            except Exception:
                pass

        # JSON-LD
        try:
            scripts = await page.query_selector_all('script[type="application/ld+json"]')
            for s in scripts:
                text = await s.inner_text()
                data = json.loads(text)
                if isinstance(data, dict) and ("offers" in data or "itemListElement" in data):
                    return data
        except Exception:
            pass

        return None


def _count_listing_items(data: Any) -> int:
    """Heuristic: count how many listing-like items are in a JSON payload."""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in (
            "listings", "items", "tickets", "rows", "results",
            "data", "offers", "events", "inventory",
        ):
            val = data.get(key)
            if isinstance(val, list) and len(val) >= 1:
                return len(val)
        for val in data.values():
            if isinstance(val, dict):
                for key2 in ("listings", "items", "tickets", "rows", "results"):
                    val2 = val.get(key2)
                    if isinstance(val2, list) and len(val2) >= 1:
                        return len(val2)
    return 0
