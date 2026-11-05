"""Settings, read from environment variables.

Every setting has a safe default so the store runs locally with no setup.
"""

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
  value = os.environ.get(name)
  if value is None:
    return default
  return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
  # Public base URL of the store, used in the discovery profile and links.
  # Leave empty to derive it from each incoming request.
  base_url: str
  # Where the SQLite database lives.
  db_path: str
  # Store name shown to buyers.
  store_name: str
  # Strict mode: reject requests without a fetchable UCP-Agent profile.
  strict_negotiation: bool
  # Allow fetching agent profiles from http:// or private network addresses
  # (local testing only).
  allow_insecure_profile_urls: bool
  # How long the mock payment "timeout" scenario waits before failing.
  mock_timeout_seconds: float
  # How long the mock "pending" payment stays in complete_in_progress.
  mock_pending_seconds: float
  # Secret for the /admin/reset endpoint; empty disables it.
  admin_token: str


def load_settings() -> Settings:
  return Settings(
    base_url=os.environ.get("BASE_URL", "").rstrip("/"),
    db_path=os.environ.get("DB_PATH", "data/store.db"),
    store_name=os.environ.get("STORE_NAME", "UCP Test Market"),
    strict_negotiation=_bool("STRICT_NEGOTIATION", False),
    allow_insecure_profile_urls=_bool("ALLOW_INSECURE_PROFILE_URLS", False),
    mock_timeout_seconds=float(os.environ.get("MOCK_TIMEOUT_SECONDS", "15")),
    mock_pending_seconds=float(os.environ.get("MOCK_PENDING_SECONDS", "10")),
    admin_token=os.environ.get("ADMIN_TOKEN", ""),
  )
