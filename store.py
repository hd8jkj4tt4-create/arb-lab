"""SQLite persistence + JSON settings."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from core import Evaluation, alert_key, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    events INTEGER NOT NULL,
    arbs INTEGER NOT NULL,
    duration_ms INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_id TEXT NOT NULL,
    league TEXT,
    home TEXT,
    away TEXT,
    commence TEXT,
    s REAL NOT NULL,
    margin REAL NOT NULL,
    status TEXT NOT NULL,
    best_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_event ON snapshots(event_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    league TEXT,
    home TEXT,
    away TEXT,
    commence TEXT,
    s REAL NOT NULL,
    margin REAL NOT NULL,
    legs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "bankroll": 1000.0,
    "threshold": 0.985,
    "min_stake": 0.5,
    "alerts": True,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._data.update(raw)
                except (OSError, json.JSONDecodeError):
                    pass
            self._data = self._sanitize(self._data)
            return dict(self._data)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = dict(self._data)
            merged.update(patch)
            self._data = self._sanitize(merged)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return dict(self._data)

    @staticmethod
    def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        try:
            out["bankroll"] = _clamp(float(data.get("bankroll", 1000)), 1.0, 10_000_000.0)
        except (TypeError, ValueError):
            pass
        try:
            out["threshold"] = _clamp(float(data.get("threshold", 0.985)), 0.90, 1.20)
        except (TypeError, ValueError):
            pass
        try:
            out["min_stake"] = _clamp(float(data.get("min_stake", 0.5)), 0.01, 100.0)
        except (TypeError, ValueError):
            pass
        out["alerts"] = bool(data.get("alerts", True))
        return out


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def record_run(self, provider: str, events: int, arbs: int, duration_ms: int, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO runs (ts, provider, events, arbs, duration_ms, error) VALUES (?,?,?,?,?,?)",
                (utcnow(), provider, events, arbs, duration_ms, error),
            )

    def record_snapshots(self, evaluations: list[Evaluation]) -> None:
        if not evaluations:
            return
        ts = utcnow()
        rows = [
            (
                ts,
                ev.event.id,
                ev.event.league,
                ev.event.home,
                ev.event.away,
                ev.event.commence,
                ev.s,
                ev.margin,
                ev.status,
                json.dumps(
                    [
                        {
                            "code": p.code,
                            "label": p.label,
                            "odds": p.odds,
                            "book": p.book_title,
                            "href": p.href,
                        }
                        for p in ev.best
                    ],
                    ensure_ascii=False,
                ),
            )
            for ev in evaluations
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO snapshots (ts, event_id, league, home, away, commence, s, margin, status, best_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def add_alert(self, ev: Evaluation) -> bool:
        key = alert_key(ev.event.id, ev.event.market, ev.margin)
        payload = json.dumps(
            [
                {
                    "code": p.code,
                    "label": p.label,
                    "odds": p.odds,
                    "book": p.book_title,
                    "href": p.href,
                }
                for p in ev.best
            ],
            ensure_ascii=False,
        )
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO alerts"
                " (ts, key, event_id, market, league, home, away, commence, s, margin, legs_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    utcnow(), key, ev.event.id, ev.event.market, ev.event.league,
                    ev.event.home, ev.event.away, ev.event.commence, ev.s, ev.margin, payload,
                ),
            )
            return cur.rowcount == 1

    def recent_alerts(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["legs"] = json.loads(item.pop("legs_json"))
            except json.JSONDecodeError:
                item["legs"] = []
            out.append(item)
        return out

    def last_alert_ts(self, event_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT ts FROM alerts WHERE event_id=? ORDER BY id DESC LIMIT 1", (event_id,)
            ).fetchone()
        return row["ts"] if row else None

    def prune(self, days: int = 7) -> None:
        cutoff = f"datetime('now','-{int(days)} days')"
        with self.connect() as conn:
            conn.execute(f"DELETE FROM snapshots WHERE ts < {cutoff}")
            conn.execute(f"DELETE FROM runs WHERE ts < {cutoff}")
