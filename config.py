"""Configuration: .env loader + typed settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env parser (no dependency). Existing env vars win."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass
class Config:
    provider: str = "demo"
    odds_api_key: str = ""
    odds_api_io_key: str = ""
    regions: str = "eu"
    bookmakers: list[str] = field(default_factory=list)
    sports: list[str] = field(default_factory=list)
    leagues: list[str] = field(default_factory=list)
    poll_seconds: int = 300
    bankroll: float = 1000.0
    threshold: float = 0.985
    min_stake: float = 0.5
    port: int = 8765
    telegram_token: str = ""
    telegram_chat: str = ""
    db_path: Path = ROOT / "data" / "arb.db"
    settings_path: Path = ROOT / "data" / "settings.json"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        cfg.provider = os.getenv("PROVIDER", "demo").strip().lower() or "demo"
        cfg.odds_api_key = os.getenv("ODDS_API_KEY", "").strip()
        cfg.odds_api_io_key = os.getenv("ODDS_API_IO_KEY", "").strip()
        cfg.regions = os.getenv("REGIONS", "eu").strip() or "eu"
        cfg.bookmakers = _csv(os.getenv("BOOKMAKERS"))
        cfg.sports = _csv(os.getenv("SPORTS")) or list(ITALY_SPORTS.keys())
        cfg.leagues = _csv(os.getenv("LEAGUES"))
        cfg.poll_seconds = max(30, int(os.getenv("POLL_SECONDS", "300")))
        cfg.bankroll = float(os.getenv("BANKROLL", "1000"))
        cfg.threshold = float(os.getenv("THRESHOLD", "0.985"))
        cfg.min_stake = float(os.getenv("MIN_STAKE", "0.5"))
        cfg.port = int(os.getenv("PORT", "8765"))
        cfg.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        cfg.telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        return cfg


# Imported late to keep Config independent from core's dataclasses.
from core import ITALY_SPORTS  # noqa: E402
