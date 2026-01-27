"""SQLite storage via SQLAlchemy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from tracker.config import settings
from tracker.models import HourlyMetrics, Listing

Base = declarative_base()


class RawListingRow(Base):
    __tablename__ = "raw_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    marketplace = Column(String(50), nullable=False, index=True)
    event_id = Column(String(255), nullable=False)
    section = Column(String(100))
    section_clean = Column(String(100))
    section_number = Column(Integer)
    level_bucket = Column(String(20))
    row = Column(String(50))
    qty = Column(Integer)
    base_price = Column(Float)
    fees = Column(Float)
    all_in_price = Column(Float)
    display_price = Column(Float)
    pricing_basis = Column(String(20), default="all_in")
    currency = Column(String(10), default="USD")
    listing_url = Column(Text)
    raw_payload_json = Column(Text)


class HourlyMetricsRow(Base):
    __tablename__ = "hourly_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    marketplace = Column(String(50), nullable=False, index=True)
    event_id = Column(String(255))
    listing_count = Column(Integer)
    get_in = Column(Float)
    mean = Column(Float)
    median = Column(Float)
    loge_best = Column(Float)
    loge_count = Column(Integer, default=0)
    pricing_basis = Column(String(20), default="all_in")
    notes = Column(Text, default="")


class EventPinRow(Base):
    """Tracks pinned event IDs per marketplace to detect unexpected changes."""
    __tablename__ = "event_pins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    marketplace = Column(String(50), nullable=False, unique=True)
    event_ref = Column(String(500), nullable=False)
    pinned_at = Column(DateTime, nullable=False)


_engine = None
_SessionLocal = None


def _get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        db_path = Path(settings.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(_engine)
        _SessionLocal = sessionmaker(bind=_engine)
    return _engine


def get_session() -> Session:
    _get_engine()
    return _SessionLocal()  # type: ignore[misc]


def store_raw_listings(listings: list[Listing]) -> None:
    session = get_session()
    try:
        for l in listings:
            session.add(
                RawListingRow(
                    timestamp=l.pulled_at_utc,
                    marketplace=l.marketplace,
                    event_id=l.event_ref,
                    section=l.section,
                    section_clean=l.section_clean,
                    section_number=l.section_number,
                    level_bucket=l.level_bucket,
                    row=l.row,
                    qty=l.quantity,
                    base_price=l.base_price,
                    fees=l.fees,
                    all_in_price=l.all_in_price,
                    display_price=l.display_price,
                    pricing_basis=l.pricing_basis,
                    currency=l.currency,
                    listing_url=l.listing_url,
                    raw_payload_json=json.dumps(l.raw_payload, default=str),
                )
            )
        session.commit()
    finally:
        session.close()


def store_hourly_metrics(metrics: list[HourlyMetrics], force: bool = False) -> None:
    session = get_session()
    try:
        for m in metrics:
            hour_ts = m.timestamp_utc.replace(minute=0, second=0, microsecond=0)
            if not force:
                existing = (
                    session.query(HourlyMetricsRow)
                    .filter(
                        HourlyMetricsRow.timestamp == hour_ts,
                        HourlyMetricsRow.marketplace == m.marketplace,
                    )
                    .first()
                )
                if existing:
                    continue
            session.add(
                HourlyMetricsRow(
                    timestamp=hour_ts,
                    marketplace=m.marketplace,
                    event_id=m.event_ref,
                    listing_count=m.listing_count,
                    get_in=m.get_in,
                    mean=m.mean,
                    median=m.median,
                    loge_best=m.loge_best,
                    loge_count=m.loge_count,
                    pricing_basis=m.pricing_basis,
                    notes=m.notes,
                )
            )
        session.commit()
    finally:
        session.close()


def get_metrics_history(days: int = 14) -> list[HourlyMetricsRow]:
    session = get_session()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = (
            session.query(HourlyMetricsRow)
            .filter(HourlyMetricsRow.timestamp >= cutoff)
            .order_by(HourlyMetricsRow.timestamp)
            .all()
        )
        session.expunge_all()
        return rows
    finally:
        session.close()


def get_previous_metrics(
    marketplace: str, hours_ago: int = 1
) -> HourlyMetricsRow | None:
    session = get_session()
    try:
        target = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(hours=hours_ago)
        row = (
            session.query(HourlyMetricsRow)
            .filter(
                HourlyMetricsRow.marketplace == marketplace,
                HourlyMetricsRow.timestamp == target,
            )
            .first()
        )
        if row:
            session.expunge(row)
        return row
    finally:
        session.close()


def pin_event_ref(marketplace: str, event_ref: str) -> str | None:
    """Pin event ref for a marketplace. Returns error string if ref changed, else None."""
    session = get_session()
    try:
        existing = (
            session.query(EventPinRow)
            .filter(EventPinRow.marketplace == marketplace)
            .first()
        )
        if existing:
            if existing.event_ref != event_ref:
                return (
                    f"Event ref changed for {marketplace}: "
                    f"pinned={existing.event_ref!r}, current={event_ref!r}"
                )
            return None
        session.add(EventPinRow(
            marketplace=marketplace,
            event_ref=event_ref,
            pinned_at=datetime.now(timezone.utc),
        ))
        session.commit()
        return None
    finally:
        session.close()
