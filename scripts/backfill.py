#!/usr/bin/env python3
"""Re-render charts and optionally rebuild metrics from raw DB data."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.charting import generate_chart
from tracker.config import settings


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


if __name__ == "__main__":
    setup_logging()
    logger = logging.getLogger("backfill")

    out = generate_chart()
    logger.info("Chart regenerated: %s", out)
