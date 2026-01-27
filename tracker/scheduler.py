"""APScheduler-based hourly scheduler with auto-stop."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from tracker.config import settings
from tracker.runner import run_once

logger = logging.getLogger(__name__)


def _stop_dt_utc() -> datetime:
    local = datetime.fromisoformat(settings.stop_at).replace(
        tzinfo=ZoneInfo(settings.event_timezone)
    )
    return local.astimezone(timezone.utc)


def _job():
    now = datetime.now(timezone.utc)
    if now > _stop_dt_utc():
        logger.info("Past STOP_AT (%s). Shutting down.", settings.stop_at)
        sys.exit(0)
    asyncio.run(run_once())


def start_scheduler() -> None:
    now = datetime.now(timezone.utc)
    if now > _stop_dt_utc():
        logger.info("Already past STOP_AT. Exiting.")
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(
        _job,
        "interval",
        minutes=settings.run_every_minutes,
        next_run_time=datetime.now(),  # run immediately on start
    )
    logger.info(
        "Scheduler started. Running every %d min until %s.",
        settings.run_every_minutes,
        settings.stop_at,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
