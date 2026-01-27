#!/usr/bin/env python3
"""One-shot: fetch → compute → chart → email."""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.config import settings
from tracker.runner import run_once


def setup_logging() -> None:
    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = settings.log_dir / "tracker.log"
    from logging.handlers import RotatingFileHandler
    handlers.append(
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    )
    logging.basicConfig(level=logging.INFO, format=log_fmt, handlers=handlers)


if __name__ == "__main__":
    setup_logging()
    force = "--force" in sys.argv
    asyncio.run(run_once(force=force))
