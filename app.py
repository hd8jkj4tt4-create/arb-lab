#!/usr/bin/env python3
"""Arb Lab — local, automatic Italian-football arbitrage scanner.

Runs a tiny stdlib HTTP server, polls an odds provider on a schedule, evaluates
every Italian football market and serves a live dashboard. No dependencies.

    python3 app.py
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from config import Config, load_dotenv
from core import SPORTS, Evaluation, implied_probabilities, utcnow
from predictions import PredictionService
from providers import DemoProvider, ProviderError, get_provider
from store import Settings, Store

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arb-lab")

load_dotenv()
CFG = Config.from_env()
SETTINGS = Settings(CFG.settings_path)
STORE = Store(CFG.db_path)
PREDICTIONS = PredictionService()

try:
    PROVIDER = get_provider(CFG)
    PROVIDER_WARNING = None
except ProviderError as exc:
    PROVIDER = DemoProvider()
    PROVIDER_WARNING = f"{exc} Falling back to demo data."

STATE: dict[str, Any] = {
    "payload": None,
    "last_error": None,
    "next_run": None,
    "last_run": None,
}
LOCK = threading.Lock()
WAKE = threading.Event()
LAST_ALERT: dict[str, float] = {}

ALERT_COOLDOWN = 30 * 60  # seconds per event


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------

def serialize(ev: Evaluation) -> dict[str, Any]:
    probs = implied_probabilities(p.odds for p in ev.best)
    best_index = max(range(len(probs)), key=probs.__getitem__) if probs else None
    best_pick = None
    if best_index is not None:
        price = ev.best[best_index]
        best_pick = {
            "index": best_index,
            "code": price.code,
            "label": price.label,
            "odds": price.odds,
            "book": price.book_title,
            "book_key": price.book_key,
            "implied_probability": round(probs[best_index], 4),
        }
    return {
        "id": ev.event.id,
        "league": ev.event.league,
        "home": ev.event.home,
        "away": ev.event.away,
        "commence": ev.event.commence,
        "market": ev.event.market,
        "s": round(ev.s, 5),
        "margin": round(ev.margin, 5),
        "status": ev.status,
        "labels": ev.labels,
        "codes": ev.codes,
        "legs": [
            {
                "code": p.code,
                "label": p.label,
                "odds": p.odds,
                "book": p.book_title,
                "book_key": p.book_key,
                "href": p.href,
                "max_stake": p.max_stake,
                "implied_probability": round(probs[i], 4) if i < len(probs) else None,
            }
            for i, p in enumerate(ev.best)
        ],
        "best_pick": best_pick,
        "stakes": ev.stakes,
        "payouts": ev.payouts,
        "total_stake": ev.total_stake,
        "min_payout": ev.min_payout,
        "profit": ev.profit,
        "capped": ev.capped,
        "warnings": ev.warnings,
    }


def telegram_send(text: str) -> None:
    if not (CFG.telegram_token and CFG.telegram_chat):
        return
    try:
        url = f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage"
        body = urllib.parse.urlencode(
            {"chat_id": CFG.telegram_chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 - alerts must never break polling
        log.warning("telegram alert failed: %s", exc)


def alert_text(ev: Evaluation) -> str:
    probs = implied_probabilities(p.odds for p in ev.best)
    best_index = max(range(len(probs)), key=probs.__getitem__) if probs else None
    lines = [
        f"<b>ARB {ev.margin * 100:+.2f}%</b> · {ev.event.league}",
        f"{ev.event.home} – {ev.event.away}",
        f"S = {ev.s:.4f} · kickoff {ev.event.commence}",
        "",
    ]
    for price, stake in zip(ev.best, ev.stakes):
        lines.append(f"{price.code} {price.book_title} @ {price.odds:.2f} → €{stake:.2f}")
    if best_index is not None:
        price = ev.best[best_index]
        lines.append(
            f"Best single: {price.code} {price.label} @ {price.odds:.2f} "
            f"({probs[best_index] * 100:.0f}% implied, not risk-free)"
        )
    lines.append(f"Stake €{ev.total_stake:.2f} · worst return €{ev.min_payout:.2f} · +€{ev.profit:.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# polling
# ---------------------------------------------------------------------------

def build_payload() -> dict[str, Any]:
    settings = SETTINGS.get()
    started = time.time()
    # The dashboard settings must drive provider-side stake scaling too.
    run_cfg = replace(
        CFG,
        bankroll=float(settings["bankroll"]),
        min_stake=float(settings["min_stake"]),
    )
    result = PROVIDER.fetch(run_cfg)
    elapsed_ms = int((time.time() - started) * 1000)

    if result.precomputed:
        evaluations = list(result.precomputed)
        events_count = len({ev.event.id for ev in evaluations})
        opportunities = len(evaluations)
    else:
        from core import find_opportunities

        evaluations = find_opportunities(
            result.events,
            bankroll=float(settings["bankroll"]),
            min_stake=float(settings["min_stake"]),
        )
        events_count = len(result.events)
        opportunities = len(evaluations)

    arbs = [ev for ev in evaluations if ev.s < 1.0 and ev.profit > 0]
    rounding = [ev for ev in evaluations if ev.s < 1.0 and ev.profit <= 0]
    near = [ev for ev in evaluations if 1.0 <= ev.s <= 1.02]
    best_s = min((ev.s for ev in arbs), default=None)

    # Persist snapshots and fire alerts for new opportunities.
    STORE.record_snapshots(evaluations)
    new_alerts: list[dict[str, Any]] = []
    if bool(settings.get("alerts", True)):
        threshold = float(settings["threshold"])
        now = time.time()
        for ev in arbs:
            if ev.s > threshold:
                continue
            last = LAST_ALERT.get(ev.event.id, 0.0)
            if now - last < ALERT_COOLDOWN:
                continue
            if STORE.add_alert(ev):
                LAST_ALERT[ev.event.id] = now
                new_alerts.append(serialize(ev))
                log.info(
                    "ALERT %s %s-%s S=%.4f margin=%+.2f%%",
                    ev.event.league, ev.event.home, ev.event.away, ev.s, ev.margin * 100,
                )
                telegram_send(alert_text(ev))

    STORE.record_run(PROVIDER.key, events_count, len(arbs), elapsed_ms)
    payload = {
        "generated_at": utcnow(),
        "provider": PROVIDER.key,
        "provider_label": PROVIDER.label,
        "demo": PROVIDER.key == "demo",
        "poll_seconds": CFG.poll_seconds,
        "settings": settings,
        "config": {
            "regions": CFG.regions,
            "bookmakers": CFG.bookmakers,
            "sports": [SPORTS.get(s, s) for s in CFG.sports],
            "predictions_refresh_hours": CFG.predictions_refresh_hours,
            "has_odds_api_key": bool(CFG.odds_api_key),
            "has_odds_api_io_key": bool(CFG.odds_api_io_key),
            "telegram": bool(CFG.telegram_token and CFG.telegram_chat),
        },
        "stats": {
            "events": events_count,
            "evaluated": len(evaluations),
            "opportunities": opportunities,
            "arbs": len(arbs),
            "near": len(near),
            "rounding": len(rounding),
            "best_s": round(best_s, 5) if best_s is not None else None,
            "best_margin": round(1.0 / best_s - 1.0, 5) if best_s else None,
        },
        "quota": result.meta.get("quota"),
        "books": result.meta.get("books") or [],
        "warnings": ([PROVIDER_WARNING] if PROVIDER_WARNING else []) + list(result.warnings),
        "opportunities": [serialize(ev) for ev in evaluations],
        "alerts": STORE.recent_alerts(30),
        "new_alerts": new_alerts,
        "error": None,
    }
    return payload


def poll_loop() -> None:
    delay = CFG.poll_seconds
    last_prune = 0.0
    while True:
        if time.time() - last_prune > 24 * 3600:
            try:
                STORE.prune(days=7)
            except Exception as exc:  # noqa: BLE001 - pruning is best effort
                log.warning("prune failed: %s", exc)
            last_prune = time.time()
        started = time.time()
        with LOCK:
            STATE["last_run"] = utcnow()
        try:
            payload = build_payload()
            with LOCK:
                STATE["payload"] = payload
                STATE["last_error"] = None
            delay = CFG.poll_seconds
            log.info(
                "poll ok: %d events, %d arbs, %.1fs",
                payload["stats"]["events"], payload["stats"]["arbs"], time.time() - started,
            )
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            message = str(exc)
            log.error("poll failed: %s", message)
            with LOCK:
                STATE["last_error"] = message
                if STATE["payload"] is not None:
                    STATE["payload"]["error"] = message
            STORE.record_run(PROVIDER.key, 0, 0, int((time.time() - started) * 1000), message[:500])
            delay = min(max(CFG.poll_seconds * 2, 120), 900)

        with LOCK:
            STATE["next_run"] = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        WAKE.wait(timeout=delay)
        WAKE.clear()


def predictions_loop() -> None:
    """Refresh the win-probability model independently from odds polling."""
    normal_delay = max(1, CFG.predictions_refresh_hours) * 3600
    while True:
        delay = normal_delay
        try:
            snapshot = PREDICTIONS.refresh()
            fixtures = sum(len(c.get("fixtures") or []) for c in snapshot.get("competitions", []))
            log.info("predictions updated: %d competitions, %d fixtures", len(snapshot.get("competitions", [])), fixtures)
        except Exception as exc:  # noqa: BLE001 - keep the thread alive
            log.warning("predictions refresh failed: %s", exc)
            delay = 900
        time.sleep(delay)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ArbLab/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    def _headers(self, status: int, content_type: str, length: int, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()

    def send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), {"Cache-Control": "no-store"})
        self.wfile.write(body)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, cache: str = "no-store") -> None:
        self._headers(status, content_type, len(body), {"Cache-Control": cache})
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if length <= 0 or length > 100_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/api/state":
            with LOCK:
                payload = STATE["payload"]
                error = STATE["last_error"]
                next_run = STATE["next_run"]
            if payload is None:
                self.send_json(
                    {
                        "loading": True,
                        "provider": PROVIDER.key,
                        "provider_label": PROVIDER.label,
                        "demo": PROVIDER.key == "demo",
                        "poll_seconds": CFG.poll_seconds,
                        "settings": SETTINGS.get(),
                        "warnings": [PROVIDER_WARNING] if PROVIDER_WARNING else [],
                        "error": error,
                        "next_run": next_run,
                        "generated_at": utcnow(),
                    }
                )
                return
            response = dict(payload)
            response["next_run"] = next_run
            response["error"] = error
            response["settings"] = SETTINGS.get()
            self.send_json(response)
            return

        if path == "/api/predictions":
            self.send_json(PREDICTIONS.snapshot())
            return

        if path == "/api/history":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["30"])[0])
            except ValueError:
                limit = 30
            self.send_json({"alerts": STORE.recent_alerts(limit)})
            return

        if path == "/api/health":
            with LOCK:
                error = STATE["last_error"]
                last_run = STATE["last_run"]
            self.send_json(
                {
                    "ok": error is None,
                    "provider": PROVIDER.key,
                    "last_run": last_run,
                    "error": error,
                    "settings": SETTINGS.get(),
                }
            )
            return

        if path in ("/", "/index.html"):
            self.serve_static("index.html")
            return

        if path.startswith("/static/"):
            self.serve_static(path[len("/static/"):])
            return

        if path == "/favicon.ico":
            self.send_bytes(b"", "image/x-icon", status=204)
            return

        self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/api/refresh":
            WAKE.set()
            PREDICTIONS.request_refresh()
            self.send_json({"ok": True, "message": "refresh requested"})
            return

        if path == "/api/predictions/refresh":
            started = PREDICTIONS.request_refresh()
            self.send_json({"ok": True, "started": started})
            return

        if path == "/api/settings":
            patch = self.read_json()
            allowed = {k: v for k, v in patch.items() if k in ("bankroll", "threshold", "min_stake", "alerts")}
            if not allowed:
                self.send_json({"error": "no valid settings in body"}, status=400)
                return
            updated = SETTINGS.update(allowed)
            WAKE.set()
            self.send_json({"ok": True, "settings": updated})
            return

        self.send_json({"error": "not found"}, status=404)

    def serve_static(self, relative: str) -> None:
        base = STATIC.resolve()
        target = (base / relative).resolve()
        if not str(target).startswith(str(base) + os.sep):
            self.send_json({"error": "forbidden"}, status=403)
            return
        if not target.is_file():
            self.send_json({"error": "not found"}, status=404)
            return
        body = target.read_bytes()
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        cache = "no-store" if target.suffix == ".html" else "no-cache"
        self.send_bytes(body, ctype, cache=cache)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("provider: %s (%s)", PROVIDER.key, PROVIDER.label)
    if PROVIDER_WARNING:
        log.warning(PROVIDER_WARNING)
    log.info("poll every %ss · http://127.0.0.1:%s", CFG.poll_seconds, CFG.port)

    thread = threading.Thread(target=poll_loop, name="poller", daemon=True)
    thread.start()

    predictions_thread = threading.Thread(target=predictions_loop, name="predictions", daemon=True)
    predictions_thread.start()

    host = os.getenv("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, CFG.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
