"""Section name normalization and level detection."""

from __future__ import annotations

import re


def normalize_section(raw: str) -> str:
    """Strip whitespace, uppercase, remove common prefixes."""
    s = raw.strip().upper()
    s = re.sub(r"^(SECTION|SECT|SEC)\.?[\s:#-]*", "", s, flags=re.I)
    s = s.strip()
    return s


def extract_section_number(section: str) -> int | None:
    """Extract the first 3-digit number anywhere in the normalized section text.

    Falls back to any leading digits if no 3-digit number found.
    """
    cleaned = normalize_section(section)
    # Prefer a 3-digit number anywhere in the text (not necessarily word-bounded,
    # since "C101" has no word boundary between C and 1).
    m = re.search(r"(?<!\d)(\d{3})(?!\d)", cleaned)
    if m:
        return int(m.group(1))
    # Fallback: any leading digits
    m = re.match(r"(\d+)", cleaned)
    return int(m.group(1)) if m else None


def is_lower_level(section: str) -> bool:
    """Return True if section number is 100-199 inclusive.

    Uses extract_section_number to find the number anywhere in the text,
    rather than requiring it to start with '1'.
    """
    num = extract_section_number(section)
    if num is None:
        return False
    return 100 <= num <= 199


def classify_level_bucket(section: str) -> str:
    """Return level bucket: 'lower_100s', 'other', or 'unknown'."""
    num = extract_section_number(section)
    if num is None:
        return "unknown"
    if 100 <= num <= 199:
        return "lower_100s"
    return "other"


def enrich_section(listing) -> None:
    """Enrich a Listing with section_raw, section_clean, section_number, level_bucket.

    Mutates the listing in place.
    """
    raw = listing.section or ""
    listing.section_raw = raw
    listing.section_clean = normalize_section(raw)
    listing.section_number = extract_section_number(raw)
    listing.level_bucket = classify_level_bucket(raw)
