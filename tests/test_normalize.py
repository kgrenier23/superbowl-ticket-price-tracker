"""Tests for tracker.normalize."""

from tracker.normalize import (
    classify_level_bucket,
    enrich_section,
    extract_section_number,
    is_lower_level,
    normalize_section,
)
from tracker.models import Listing
from datetime import datetime, timezone


class TestNormalizeSection:
    def test_strips_prefix(self):
        assert normalize_section("Section 134") == "134"
        assert normalize_section("SEC 201") == "201"
        assert normalize_section("Sect. 112") == "112"

    def test_uppercases(self):
        assert normalize_section("club level") == "CLUB LEVEL"

    def test_already_clean(self):
        assert normalize_section("101") == "101"

    def test_whitespace(self):
        assert normalize_section("  Section  305  ") == "305"


class TestIsLowerLevel:
    def test_100s(self):
        assert is_lower_level("101") is True
        assert is_lower_level("112") is True
        assert is_lower_level("134") is True
        assert is_lower_level("199") is True
        assert is_lower_level("Section 142") is True

    def test_200s_300s(self):
        assert is_lower_level("201") is False
        assert is_lower_level("305") is False
        assert is_lower_level("Section 250") is False

    def test_non_numeric(self):
        assert is_lower_level("Club") is False

    def test_c_prefix_with_100s(self):
        # "C101" — the 3-digit 101 is found by search
        assert is_lower_level("C101") is True

    def test_edge_cases(self):
        assert is_lower_level("1") is False
        assert is_lower_level("10") is False
        assert is_lower_level("100") is True
        assert is_lower_level("1000") is False


class TestExtractSectionNumber:
    def test_numeric(self):
        assert extract_section_number("134") == 134
        assert extract_section_number("Section 201") == 201

    def test_non_numeric(self):
        assert extract_section_number("Club") is None

    def test_embedded_number(self):
        # Finds 3-digit number anywhere in text
        assert extract_section_number("C110") == 110
        assert extract_section_number("Suite 150") == 150


class TestClassifyLevelBucket:
    def test_lower(self):
        assert classify_level_bucket("101") == "lower_100s"
        assert classify_level_bucket("Section 142") == "lower_100s"

    def test_other(self):
        assert classify_level_bucket("201") == "other"
        assert classify_level_bucket("305") == "other"

    def test_unknown(self):
        assert classify_level_bucket("Club") == "unknown"


class TestEnrichSection:
    def test_enrichment(self):
        l = Listing(
            marketplace="test",
            pulled_at_utc=datetime.now(timezone.utc),
            event_ref="e1",
            section="Section 134",
        )
        enrich_section(l)
        assert l.section_raw == "Section 134"
        assert l.section_clean == "134"
        assert l.section_number == 134
        assert l.level_bucket == "lower_100s"

    def test_non_numeric_section(self):
        l = Listing(
            marketplace="test",
            pulled_at_utc=datetime.now(timezone.utc),
            event_ref="e1",
            section="Club Level",
        )
        enrich_section(l)
        assert l.section_clean == "CLUB LEVEL"
        assert l.section_number is None
        assert l.level_bucket == "unknown"
