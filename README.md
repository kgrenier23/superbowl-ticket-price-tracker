# Super Bowl Ticket Price Tracker

Hourly tracker for Super Bowl ticket prices across 5 marketplaces. Computes get-in, median, mean, and lower-level best prices, then emails a summary with CSV + trend chart PNG.

## Marketplaces

| Source | Mode | Notes |
|--------|------|-------|
| SeatGeek | **Tier A**: API (with key), **Tier B**: network capture | Set `SEATGEEK_API_KEY` for API mode |
| StubHub | **Tier A**: OAuth2 API, **Tier B**: network capture from event page | Set `STUBHUB_CLIENT_ID` + `STUBHUB_CLIENT_SECRET` for API |
| Vivid Seats | **Tier B**: network capture from event page | Requires `VIVIDSEATS_EVENT_URL` |
| TickPick | **Tier B**: network capture from event page | All-in pricing (no fees). Requires `TICKPICK_EVENT_URL` |
| On Location | **Tier B**: network capture from event page | Hospitality packages; tracked separately from resale rollups |

### Extraction Tiers

- **Tier A** — Official API (when credentials present)
- **Tier B** — Playwright event-page load + network response JSON capture (preferred for non-public APIs)
- **Tier C** — Embedded page state fallback (`__NEXT_DATA__`, `__APOLLO_STATE__`, JSON-LD)

## Setup

```bash
# Clone and install
pip install -e ".[dev]"
playwright install chromium
```

Copy `.env.example` to `.env` and fill in values.

## Configuration

All config is via environment variables (see `.env.example`):

| Variable | Description |
|----------|-------------|
| `EVENT_DATETIME` | Event date/time (ISO 8601) |
| `EVENT_TIMEZONE` | IANA timezone |
| `STOP_AT` | Stop scheduling after this datetime |
| `DRY_RUN` | `true` to print email instead of sending |
| `DEBUG_CAPTURE` | `1` to save debug artifacts (screenshots, HTML, network logs) on failures |
| `SMTP_*` | SMTP server credentials |
| `EMAIL_FROM` / `EMAIL_TO` | Sender and recipients |
| `SEATGEEK_API_KEY` | Optional; enables API mode for SeatGeek |
| `SEATGEEK_EVENT_URL` | Direct event page URL for SeatGeek |
| `STUBHUB_EVENT_URL` | Direct event page URL for StubHub |
| `STUBHUB_CLIENT_ID` / `STUBHUB_CLIENT_SECRET` | Optional; enables OAuth2 API mode |
| `VIVIDSEATS_EVENT_URL` | Direct event page URL for Vivid Seats |
| `TICKPICK_EVENT_URL` | Direct event page URL for TickPick |
| `ONLOCATION_EVENT_URL` | Direct event page URL for On Location |

### Event URLs (Required for Reliable Tracking)

Set explicit event URLs per marketplace. Without these, connectors fall back to search which is unreliable.

```env
SEATGEEK_EVENT_URL=https://seatgeek.com/super-bowl-tickets/2-8-2026-santa-clara-california-levi-s-stadium/nfl/17382581
STUBHUB_EVENT_URL=https://www.stubhub.com/super-bowl-santa-clara-tickets-2-8-2026/event/157245215/
VIVIDSEATS_EVENT_URL=https://www.vividseats.com/super-bowl-lx-tickets/...
TICKPICK_EVENT_URL=https://www.tickpick.com/buy-super-bowl-lx-tickets/...
ONLOCATION_EVENT_URL=https://onlocationexp.com/nfl/super-bowl/tickets
```

### Event ID Pinning

The first time a connector resolves an event ref, it is pinned in the database. If the ref changes on a subsequent run (e.g., discovery picked a different event), that connector fails and the email alerts you.

## Run

```bash
# One-shot (fetch + compute + chart + email)
python scripts/run_once.py

# One-shot with force (overwrite existing hourly metrics)
python scripts/run_once.py --force

# Hourly scheduler (runs until STOP_AT)
python -c "from tracker.scheduler import start_scheduler; start_scheduler()"

# Regenerate chart from existing DB data
python scripts/backfill.py
```

## Debug

```bash
# Debug a single connector in isolation
# Saves screenshots, HTML, network logs, and inventory JSON to ./debug/
DEBUG_CAPTURE=1 python scripts/debug_connector.py --marketplace stubhub --url "https://www.stubhub.com/super-bowl-santa-clara-tickets-2-8-2026/event/157245215/"

# Debug SeatGeek (uses configured event URL)
DEBUG_CAPTURE=1 python scripts/debug_connector.py --marketplace seatgeek

# Debug with verbose logging
DEBUG_CAPTURE=1 python scripts/debug_connector.py -m vividseats -u "https://..."
```

Debug artifacts are saved to `./debug/{marketplace}/{timestamp}/`:
- `screenshot.png` — full-page screenshot
- `page.html` — page source
- `network_summary.json` — all network responses (URL, status, content-type)
- `inventory.json` — captured inventory payload (if found)

## Docker

```bash
docker build -t sb-tracker .
docker run --env-file .env sb-tracker
# For scheduler:
docker run --env-file .env sb-tracker python -c "from tracker.scheduler import start_scheduler; start_scheduler()"
```

## Tests

```bash
pytest tests/ -v
```

Tests include:
- **Compute tests**: metrics calculation, dual ALL rollups, anomaly detection
- **Connector JSON parsing tests**: inventory payload parsing from JSON fixtures (no live web calls)
- **Normalize tests**: section normalization, lower-level detection, level bucketing
- **Legacy HTML fixture tests**: backward-compat DOM parsing tests

## Output

- `./out/superbowl_prices_snapshot_*.csv` — per-run CSV with pricing_basis column
- `./out/superbowl_prices_trend_*.png` — two-panel trend chart (get-in + lower-level by marketplace)
- `./data/tracker.db` — SQLite database (raw listings + hourly metrics + event pins)
- `./logs/tracker.log` — rotating log file
- `./debug/` — debug artifacts (when `DEBUG_CAPTURE=1`)

## Metrics & Rollups

### Per-Marketplace Metrics
- **get_in**: cheapest listing price
- **mean**: average price
- **median**: median price
- **loge_best**: cheapest lower-level (sections 100-199) listing
- **pricing_basis**: `all_in`, `base_only`, or `mixed`

### ALL Rollups (resale only, excludes On Location)
- **ALL_LISTING_WEIGHTED**: merge all listings across marketplaces, compute aggregate metrics
- **ALL_MARKET_NEUTRAL**: equal weight per marketplace — median of medians, mean of means, min of get-ins

### On Location
Tracked as a separate row with `notes="package/hospitality; not directly comparable"`. Not included in ALL rollups.

### Sanity Rules
- 0 listings = connector failure (not written to metrics unless sold-out confirmed)
- Get-in change >25% vs previous hour = flagged as "large move" in email
- Listing count drop to near-zero = flagged in email

## Architecture

Each marketplace is a "connector" inheriting from `BaseConnector`. To add a new source:
1. Create a file in `tracker/connectors/`
2. Implement `_fetch()` and `resolve_event_ref()`
3. Add it to `ALL_CONNECTORS` in `__init__.py`

Connectors run concurrently via `asyncio.gather` with a semaphore. Failures are isolated per-connector — partial results still produce metrics and emails.

### Price Clarity
Each listing tracks:
- `display_price` — what the page shows
- `all_in_price` — preferred metric price (includes fees)
- `base_price` — price before fees
- `pricing_basis` — `"all_in"` or `"base_only"`

### Section Enrichment
Each listing is enriched with:
- `section_raw` — original text
- `section_clean` — normalized (uppercase, prefixes stripped)
- `section_number` — extracted 3-digit number (or None)
- `level_bucket` — `"lower_100s"`, `"other"`, or `"unknown"`
