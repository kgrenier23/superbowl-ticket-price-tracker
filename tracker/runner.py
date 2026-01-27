"""Orchestration for a single tracker run."""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tracker.charting import generate_chart
from tracker.compute import (
    check_anomalies,
    compute_combined_listing_weighted,
    compute_combined_market_neutral,
    compute_metrics,
)
from tracker.config import settings
from tracker.connectors import ALL_CONNECTORS, BaseConnector
from tracker.emailer import send_email
from tracker.models import ConnectorStatus, HourlyMetrics, Listing
from tracker.normalize import enrich_section
from tracker.storage import (
    get_previous_metrics,
    pin_event_ref,
    store_hourly_metrics,
    store_raw_listings,
)

logger = logging.getLogger(__name__)


def _event_dt_utc() -> datetime:
    local = datetime.fromisoformat(settings.event_datetime).replace(
        tzinfo=ZoneInfo(settings.event_timezone)
    )
    return local.astimezone(timezone.utc)


def _days_until(now_utc: datetime) -> float:
    delta = _event_dt_utc() - now_utc
    return delta.total_seconds() / 86400


async def _fetch_all(
    connectors: list[BaseConnector],
    event_refs: dict[str, str],
) -> tuple[dict[str, list[Listing]], dict[str, str]]:
    """Fetch from all connectors concurrently."""
    sem = asyncio.Semaphore(settings.max_concurrent_connectors)
    results: dict[str, list[Listing]] = {}
    errors: dict[str, str] = {}

    async def _run(c: BaseConnector) -> None:
        async with sem:
            ref = event_refs.get(c.name, "")
            if not ref:
                errors[c.name] = "no event ref resolved"
                return
            try:
                results[c.name] = await c.fetch_listings(ref)
            except Exception as exc:
                errors[c.name] = str(exc)
                logger.error("Connector %s failed: %s", c.name, exc)

    await asyncio.gather(*[_run(c) for c in connectors])
    return results, errors


def _resolve_refs(connectors: list[BaseConnector]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for c in connectors:
        try:
            refs[c.name] = c.resolve_event_ref()
        except Exception as exc:
            logger.warning("Could not resolve event for %s: %s", c.name, exc)
    return refs


def _find_cheapest(listings: list[Listing], lower_only: bool = False) -> Listing | None:
    candidates = listings
    if lower_only:
        candidates = [l for l in listings if l.level_bucket == "lower_100s"]
    priced = [l for l in candidates if l.effective_price is not None]
    if not priced:
        return None
    return min(priced, key=lambda l: l.effective_price)


def _write_csv(
    metrics: list[HourlyMetrics],
    statuses: dict[str, ConnectorStatus],
    now: datetime,
    days_until: float,
) -> Path:
    path = settings.out_dir / f"superbowl_prices_snapshot_{now:%Y%m%d_%H%M}_UTC.csv"
    hours_until = days_until * 24

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp_utc", "days_until_event", "hours_until_event",
            "marketplace", "listing_count", "pricing_basis",
            "get_in_price", "median_price", "mean_price",
            "loge_best_price", "loge_listing_count",
            "currency", "notes",
        ])
        for m in metrics:
            writer.writerow([
                m.timestamp_utc.isoformat(),
                f"{days_until:.2f}",
                f"{hours_until:.1f}",
                m.marketplace,
                m.listing_count,
                m.pricing_basis,
                m.get_in if m.get_in is not None else "",
                m.median if m.median is not None else "",
                m.mean if m.mean is not None else "",
                m.loge_best if m.loge_best is not None else "",
                m.loge_count,
                "USD",
                m.notes,
            ])
    return path


def _format_delta(current: float | None, previous: float | None) -> str:
    if current is None or previous is None:
        return "N/A"
    d = current - previous
    sign = "+" if d >= 0 else ""
    return f"{sign}${d:,.0f}"


def _format_price(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.0f}"


def _build_email_body(
    metrics: list[HourlyMetrics],
    errors: dict[str, str],
    days_until: float,
    per_marketplace: dict[str, list[Listing]],
    statuses: dict[str, ConnectorStatus],
    anomalies: dict[str, list[str]],
) -> str:
    lines: list[str] = []
    lines.append(f"Super Bowl Ticket Tracker -- {days_until:.1f} days out\n")

    # Main metrics table
    lines.append(
        f"{'Marketplace':<22} {'#':>6} {'Basis':<10} {'Get-In':>10} "
        f"{'Median':>10} {'Mean':>10} {'Lower Best':>10}"
    )
    lines.append("-" * 90)
    for m in metrics:
        lines.append(
            f"{m.marketplace:<22} {m.listing_count:>6} {m.pricing_basis:<10} "
            f"{_format_price(m.get_in):>10} "
            f"{_format_price(m.median):>10} "
            f"{_format_price(m.mean):>10} "
            f"{_format_price(m.loge_best):>10}"
        )

    # Base-only callout
    base_only_markets = [
        s.marketplace for s in statuses.values() if s.pricing_basis == "base_only"
    ]
    if base_only_markets:
        lines.append(f"\n** BASE-ONLY pricing (fees not included): {', '.join(base_only_markets)}")

    # Anomaly alerts
    all_anomalies = [f for flags in anomalies.values() for f in flags]
    if all_anomalies:
        lines.append("\n--- ALERTS ---")
        for flag in all_anomalies:
            lines.append(f"  ! {flag}")

    # Deltas
    lines.append("\n--- Deltas ---")
    for m in metrics:
        if m.marketplace.startswith("ALL_"):
            continue
        prev_1h = get_previous_metrics(m.marketplace, hours_ago=1)
        prev_24h = get_previous_metrics(m.marketplace, hours_ago=24)
        d1_gi = _format_delta(m.get_in, prev_1h.get_in if prev_1h else None)
        d24_gi = _format_delta(m.get_in, prev_24h.get_in if prev_24h else None)
        d1_lg = _format_delta(m.loge_best, prev_1h.loge_best if prev_1h else None)
        d24_lg = _format_delta(m.loge_best, prev_24h.loge_best if prev_24h else None)
        lines.append(
            f"  {m.marketplace:<18} Get-In d1h={d1_gi}  d24h={d24_gi}  |  Lower d1h={d1_lg}  d24h={d24_lg}"
        )

    # Cheapest listings
    lines.append("\n--- Cheapest Listings ---")
    for name, listings in per_marketplace.items():
        cheapest = _find_cheapest(listings)
        cheapest_lower = _find_cheapest(listings, lower_only=True)
        if cheapest:
            lines.append(
                f"  {name} cheapest: {_format_price(cheapest.effective_price)} "
                f"Sec {cheapest.section_clean or cheapest.section} "
                f"Row {cheapest.row or '?'} "
                f"Qty {cheapest.quantity or '?'}"
            )
        if cheapest_lower:
            lines.append(
                f"  {name} lower-level: {_format_price(cheapest_lower.effective_price)} "
                f"Sec {cheapest_lower.section_clean or cheapest_lower.section} "
                f"Row {cheapest_lower.row or '?'} "
                f"Qty {cheapest_lower.quantity or '?'}"
            )

    # Connector status table
    lines.append("\n--- Connector Status ---")
    lines.append(f"  {'Marketplace':<18} {'Status':<8} {'Count':>6} {'Basis':<10} {'Error'}")
    lines.append("  " + "-" * 70)
    for name, st in statuses.items():
        status_str = "OK" if st.ok else "FAIL"
        lines.append(
            f"  {name:<18} {status_str:<8} {st.listing_count:>6} {st.pricing_basis:<10} {st.error[:40]}"
        )

    if errors:
        lines.append("\n--- Connector Errors ---")
        for name, err in errors.items():
            lines.append(f"  {name}: {err}")

    return "\n".join(lines)


def _build_email_body_html(
    metrics: list[HourlyMetrics],
    errors: dict[str, str],
    days_until: float,
    per_marketplace: dict[str, list[Listing]],
    statuses: dict[str, ConnectorStatus],
    anomalies: dict[str, list[str]],
) -> str:
    h: list[str] = []
    h.append(
        "<html><body style='font-family:Arial,sans-serif;color:#222;max-width:800px;margin:auto'>"
    )
    h.append(f"<h2>Super Bowl Ticket Tracker &mdash; {days_until:.1f} days out</h2>")

    # --- Metrics table ---
    h.append(
        "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
        "<thead><tr style='background:#333;color:#fff'>"
    )
    for col in ["Marketplace", "#", "Basis", "Get-In", "Median", "Mean", "Lower Best"]:
        align = "right" if col not in ("Marketplace", "Basis") else "left"
        h.append(f"<th style='padding:6px 10px;text-align:{align}'>{col}</th>")
    h.append("</tr></thead><tbody>")

    for i, m in enumerate(metrics):
        bg = "#f9f9f9" if i % 2 else "#ffffff"
        h.append(f"<tr style='background:{bg}'>")
        h.append(f"<td style='padding:4px 10px'>{m.marketplace}</td>")
        h.append(f"<td style='padding:4px 10px;text-align:right'>{m.listing_count}</td>")
        h.append(f"<td style='padding:4px 10px'>{m.pricing_basis}</td>")
        for val in [m.get_in, m.median, m.mean, m.loge_best]:
            h.append(f"<td style='padding:4px 10px;text-align:right'>{_format_price(val)}</td>")
        h.append("</tr>")
    h.append("</tbody></table>")

    # Base-only callout
    base_only = [s.marketplace for s in statuses.values() if s.pricing_basis == "base_only"]
    if base_only:
        h.append(f"<p><strong>** BASE-ONLY pricing (fees not included):</strong> {', '.join(base_only)}</p>")

    # Alerts
    all_anomalies = [f for flags in anomalies.values() for f in flags]
    if all_anomalies:
        h.append("<h3 style='color:#c00'>Alerts</h3><ul>")
        for flag in all_anomalies:
            h.append(f"<li>{flag}</li>")
        h.append("</ul>")

    # Deltas
    h.append("<h3>Deltas</h3><table style='border-collapse:collapse;font-size:13px'>")
    h.append(
        "<tr style='background:#333;color:#fff'>"
        "<th style='padding:4px 8px'>Marketplace</th>"
        "<th style='padding:4px 8px'>Get-In Δ1h</th><th style='padding:4px 8px'>Get-In Δ24h</th>"
        "<th style='padding:4px 8px'>Lower Δ1h</th><th style='padding:4px 8px'>Lower Δ24h</th></tr>"
    )
    for m in metrics:
        if m.marketplace.startswith("ALL_"):
            continue
        prev_1h = get_previous_metrics(m.marketplace, hours_ago=1)
        prev_24h = get_previous_metrics(m.marketplace, hours_ago=24)
        d1_gi = _format_delta(m.get_in, prev_1h.get_in if prev_1h else None)
        d24_gi = _format_delta(m.get_in, prev_24h.get_in if prev_24h else None)
        d1_lg = _format_delta(m.loge_best, prev_1h.loge_best if prev_1h else None)
        d24_lg = _format_delta(m.loge_best, prev_24h.loge_best if prev_24h else None)
        h.append(
            f"<tr><td style='padding:4px 8px'>{m.marketplace}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{d1_gi}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{d24_gi}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{d1_lg}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{d24_lg}</td></tr>"
        )
    h.append("</table>")

    # Cheapest listings
    h.append("<h3>Cheapest Listings</h3><ul>")
    for name, listings in per_marketplace.items():
        cheapest = _find_cheapest(listings)
        cheapest_lower = _find_cheapest(listings, lower_only=True)
        if cheapest:
            h.append(
                f"<li><strong>{name}</strong> cheapest: {_format_price(cheapest.effective_price)} "
                f"Sec {cheapest.section_clean or cheapest.section} "
                f"Row {cheapest.row or '?'} Qty {cheapest.quantity or '?'}</li>"
            )
        if cheapest_lower:
            h.append(
                f"<li><strong>{name}</strong> lower-level: {_format_price(cheapest_lower.effective_price)} "
                f"Sec {cheapest_lower.section_clean or cheapest_lower.section} "
                f"Row {cheapest_lower.row or '?'} Qty {cheapest_lower.quantity or '?'}</li>"
            )
    h.append("</ul>")

    # Connector status
    h.append("<h3>Connector Status</h3><table style='border-collapse:collapse;font-size:13px'>")
    h.append(
        "<tr style='background:#333;color:#fff'>"
        "<th style='padding:4px 8px'>Marketplace</th><th style='padding:4px 8px'>Status</th>"
        "<th style='padding:4px 8px'>Count</th><th style='padding:4px 8px'>Basis</th>"
        "<th style='padding:4px 8px'>Error</th></tr>"
    )
    for name, st in statuses.items():
        color = "#080" if st.ok else "#c00"
        label = "OK" if st.ok else "FAIL"
        h.append(
            f"<tr><td style='padding:4px 8px'>{name}</td>"
            f"<td style='padding:4px 8px;color:{color};font-weight:bold'>{label}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{st.listing_count}</td>"
            f"<td style='padding:4px 8px'>{st.pricing_basis}</td>"
            f"<td style='padding:4px 8px'>{st.error[:40]}</td></tr>"
        )
    h.append("</table>")

    if errors:
        h.append("<h3>Connector Errors</h3><ul>")
        for name, err in errors.items():
            h.append(f"<li><strong>{name}:</strong> {err}</li>")
        h.append("</ul>")

    # Inline chart
    h.append("<h3>Trend Chart</h3>")
    h.append('<img src="cid:chart" style="max-width:100%;height:auto" alt="Price trend chart">')

    h.append("</body></html>")
    return "\n".join(h)


async def run_once(force: bool = False) -> None:
    """Execute one full tracker cycle: fetch -> compute -> store -> chart -> email."""
    now = datetime.now(timezone.utc)
    days_until = _days_until(now)
    logger.info("=== Tracker run at %s (%.1f days until event) ===", now.isoformat(), days_until)

    connectors = [cls() for cls in ALL_CONNECTORS]
    event_refs = _resolve_refs(connectors)
    logger.info("Resolved event refs: %s", event_refs)

    # Pin event refs
    pin_errors: dict[str, str] = {}
    for name, ref in event_refs.items():
        err = pin_event_ref(name, ref)
        if err:
            pin_errors[name] = err
            logger.error("Event pin mismatch: %s", err)

    per_marketplace, errors = await _fetch_all(connectors, event_refs)

    # Merge pin errors
    for name, err in pin_errors.items():
        errors[name] = err
        per_marketplace.pop(name, None)

    # Enrich sections
    all_listings: list[Listing] = []
    for name, listings in per_marketplace.items():
        for l in listings:
            enrich_section(l)
        all_listings.extend(listings)
    store_raw_listings(all_listings)

    # Build connector statuses and compute per-marketplace metrics
    statuses: dict[str, ConnectorStatus] = {}
    event_ref = next(iter(event_refs.values()), "")
    per_marketplace_metrics: dict[str, HourlyMetrics] = {}
    metrics: list[HourlyMetrics] = []
    anomalies: dict[str, list[str]] = {}

    for name, listings in per_marketplace.items():
        count = len([l for l in listings if l.effective_price is not None])

        # Sanity: 0 listings = failure
        if count == 0 and name not in errors:
            errors[name] = "0 listings returned (possible scrape failure)"
            statuses[name] = ConnectorStatus(
                marketplace=name, ok=False, listing_count=0,
                error="0 listings (scrape failure?)",
            )
            continue

        m = compute_metrics(listings, name, now, event_ref)
        per_marketplace_metrics[name] = m
        metrics.append(m)

        # Anomaly check
        prev = get_previous_metrics(name, hours_ago=1)
        flags = check_anomalies(m, prev)
        if flags:
            anomalies[name] = flags

        statuses[name] = ConnectorStatus(
            marketplace=name, ok=True, listing_count=m.listing_count,
            pricing_basis=m.pricing_basis,
        )

    for name in errors:
        if name not in statuses:
            statuses[name] = ConnectorStatus(
                marketplace=name, ok=False, error=errors[name][:200],
            )

    # ALL rollups (resale only, excludes onlocation)
    lw = compute_combined_listing_weighted(per_marketplace, now, event_ref)
    metrics.append(lw)

    mn = compute_combined_market_neutral(per_marketplace_metrics, now, event_ref)
    metrics.append(mn)

    # OnLocation note
    if "onlocation" in per_marketplace_metrics:
        per_marketplace_metrics["onlocation"].notes = (
            "package/hospitality; not directly comparable"
        )

    store_hourly_metrics(metrics, force=force)

    # CSV
    csv_path = _write_csv(metrics, statuses, now, days_until)
    logger.info("CSV written: %s", csv_path)

    # Chart
    chart_path = generate_chart()
    logger.info("Chart written: %s", chart_path)

    # Email
    subject = f"Super Bowl Ticket Tracker -- {now:%Y-%m-%d %H:%M} UTC -- {days_until:.1f} days out"
    body = _build_email_body(metrics, errors, days_until, per_marketplace, statuses, anomalies)
    html_body = _build_email_body_html(metrics, errors, days_until, per_marketplace, statuses, anomalies)
    inline_images: dict[str, Path] = {}
    if chart_path and chart_path.exists():
        inline_images["chart"] = chart_path
    send_email(
        subject, body,
        attachments=[csv_path, chart_path],
        html_body=html_body,
        inline_images=inline_images,
    )

    logger.info("=== Run complete ===")
