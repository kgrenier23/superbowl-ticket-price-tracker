"""Connector parsing tests using JSON inventory fixtures (no live web calls).

Tests the network-capture JSON parsing path for each connector.
Also retains legacy HTML fixture tests for backward compatibility.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from tracker.models import Listing

FIXTURES = Path(__file__).parent / "fixtures"


# =========================================================================
# JSON inventory payload parsing tests (Tier B capture path)
# =========================================================================

class TestSeatGeekJsonParsing:
    def test_parse_inventory_payload(self):
        from tracker.connectors.seatgeek import SeatGeekConnector
        connector = SeatGeekConnector()
        data = json.loads((FIXTURES / "seatgeek_inventory.json").read_text())
        now = datetime.now(timezone.utc)

        listings = connector._parse_inventory_payload(data, "https://seatgeek.com/e/123", now)
        assert len(listings) == 5
        assert listings[0].section == "134"
        assert listings[0].all_in_price == 8500.0
        assert listings[0].base_price == 7200.0
        assert listings[0].pricing_basis == "all_in"
        assert listings[0].row == "12"
        assert listings[0].quantity == 2
        assert listings[0].level_bucket == "lower_100s"
        assert listings[3].level_bucket == "other"  # 305


class TestStubHubJsonParsing:
    def test_parse_network_payload(self):
        from tracker.connectors.stubhub import StubHubConnector
        connector = StubHubConnector()
        data = json.loads((FIXTURES / "stubhub_inventory.json").read_text())
        now = datetime.now(timezone.utc)

        listings = connector._parse_network_payload(data, "https://stubhub.com/event/123", now)
        assert len(listings) == 3
        assert listings[0].section == "101"
        assert listings[0].all_in_price == 9800.0
        assert listings[0].base_price == 8500.0
        assert listings[0].pricing_basis == "all_in"
        assert listings[0].level_bucket == "lower_100s"
        assert listings[1].level_bucket == "other"  # 215


class TestVividSeatsJsonParsing:
    def test_parse_payload(self):
        from tracker.connectors.vividseats import VividSeatsConnector
        connector = VividSeatsConnector()
        data = json.loads((FIXTURES / "vividseats_inventory.json").read_text())
        now = datetime.now(timezone.utc)

        listings = connector._parse_payload(data, "https://vividseats.com/event", now)
        assert len(listings) == 2
        assert listings[0].section == "120"
        assert listings[0].all_in_price == 8200.0
        assert listings[0].level_bucket == "lower_100s"


class TestTickPickJsonParsing:
    def test_parse_payload(self):
        from tracker.connectors.tickpick import TickPickConnector
        connector = TickPickConnector()
        data = json.loads((FIXTURES / "tickpick_inventory.json").read_text())
        now = datetime.now(timezone.utc)

        listings = connector._parse_payload(data, "https://tickpick.com/event", now)
        assert len(listings) == 2
        assert listings[0].section == "142"
        assert listings[0].all_in_price == 7900.0
        assert listings[0].fees == 0.0
        assert listings[0].pricing_basis == "all_in"
        assert listings[0].level_bucket == "lower_100s"


class TestOnLocationJsonParsing:
    def test_parse_payload(self):
        from tracker.connectors.onlocation import OnLocationConnector
        connector = OnLocationConnector()
        data = json.loads((FIXTURES / "onlocation_inventory.json").read_text())
        now = datetime.now(timezone.utc)

        listings = connector._parse_payload(data, "https://onlocationexp.com/nfl", now)
        assert len(listings) == 2
        assert listings[0].section == "110"
        assert listings[0].base_price == 12500.0
        assert listings[0].pricing_basis == "base_only"
        assert listings[0].all_in_price is None


# =========================================================================
# Legacy HTML fixture tests
# =========================================================================

def _parse_fixture_cards(html_path: Path) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_path.read_text(), "html.parser")
    cards = (
        soup.select("[data-testid='listing-card']")
        or soup.select("[class*='ListingCard']")
        or soup.select("[class*='TicketRow']")
        or soup.select("[class*='listingRow']")
        or soup.select("[class*='PackageCard']")
    )
    results = []
    for card in cards:
        text = card.get_text("\n")
        section = ""
        price = None
        sec_m = re.search(r"(?:Section|Sec)\s*(\w+)", text, re.I)
        if sec_m:
            section = sec_m.group(1)
        price_m = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
        if price_m:
            price = float(price_m[-1].replace("$", "").replace(",", ""))
        results.append({"section": section, "price": price})
    return results


class TestSeatGeekFixture:
    def test_parse(self):
        cards = _parse_fixture_cards(FIXTURES / "seatgeek_listings.html")
        assert len(cards) == 4
        assert cards[0]["section"] == "134"
        assert cards[0]["price"] == 8500.0
        assert cards[3]["price"] == 4300.0


class TestStubHubFixture:
    def test_parse(self):
        cards = _parse_fixture_cards(FIXTURES / "stubhub_listings.html")
        assert len(cards) == 3
        assert cards[0]["section"] == "101"
        assert cards[0]["price"] == 9800.0


class TestVividSeatsFixture:
    def test_parse(self):
        cards = _parse_fixture_cards(FIXTURES / "vividseats_listings.html")
        assert len(cards) == 2
        assert cards[0]["section"] == "120"
        assert cards[0]["price"] == 8200.0


class TestTickPickFixture:
    def test_parse(self):
        cards = _parse_fixture_cards(FIXTURES / "tickpick_listings.html")
        assert len(cards) == 2
        assert cards[0]["section"] == "142"
        assert cards[0]["price"] == 7900.0


class TestOnLocationFixture:
    def test_parse(self):
        cards = _parse_fixture_cards(FIXTURES / "onlocation_listings.html")
        assert len(cards) == 2
        assert cards[0]["section"] == "110"
        assert cards[0]["price"] == 12500.0
