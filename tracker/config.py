"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Event
    event_name: str = "Super Bowl"
    event_datetime: str = "2026-02-08T18:30:00"
    event_timezone: str = "America/New_York"
    stop_at: str = "2026-02-07T23:59:00"
    run_every_minutes: int = 60

    # Email / SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    email_from: str = ""
    email_to: str = ""  # comma-separated

    # Dry-run
    dry_run: bool = True

    # DB
    db_path: str = str(_PROJECT_ROOT / "data" / "tracker.db")

    # Marketplace IDs / URLs
    seatgeek_event_id: str = ""
    seatgeek_event_url: str = ""
    seatgeek_api_key: str = ""
    stubhub_client_id: str = ""
    stubhub_client_secret: str = ""
    stubhub_event_id: str = ""
    stubhub_event_url: str = ""
    vividseats_event_url: str = ""
    tickpick_event_url: str = ""
    onlocation_event_url: str = ""

    # Debug
    debug_capture: bool = False

    # Concurrency
    max_concurrent_connectors: int = 3

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # --- derived helpers ---

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.event_timezone)

    @property
    def email_recipients(self) -> list[str]:
        return [e.strip() for e in self.email_to.split(",") if e.strip()]

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    @property
    def out_dir(self) -> Path:
        d = _PROJECT_ROOT / "out"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def log_dir(self) -> Path:
        d = _PROJECT_ROOT / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


settings = Settings()
