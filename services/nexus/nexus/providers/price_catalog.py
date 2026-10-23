"""Built-in model prices, maintained with NEXUS and shipped in app updates.

For the models listed here these rates are used instead of owner-entered ones: they come from the provider's own
pricing page and follow its time-of-day schedule. Each call is priced at the moment it started.

Verified 2026-10-23 against https://api-docs.deepseek.com/quick_start/pricing, and against the owner's DeepSeek balance:
one off-peak run billed 144,768 cache-miss + 458,752 cache-hit input and 11,564 output tokens; these rates give
$0.1285, and the balance fell by $0.13.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

import httpx

CATALOG_VERSION = "2026-10-23"

# USD per million tokens.
_DEEPSEEK_PRO = {"peak": {"in": 1.32, "cached_in": 0.044, "out": 3.96},
                 "offpeak": {"in": 0.66, "cached_in": 0.022, "out": 1.98}}
_DEEPSEEK_FLASH = {"peak": {"in": 0.30, "cached_in": 0.006, "out": 1.20},
                   "offpeak": {"in": 0.15, "cached_in": 0.003, "out": 0.60}}

CATALOG: list[dict[str, Any]] = [
    {
        "provider": "DeepSeek",
        "hosts": ("api.deepseek.com", "deepseek.com"),
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
        # Peak: 01:00-04:00 and 06:00-10:00 UTC, Monday-Friday. Chinese public holidays are off-peak there; NEXUS
        # does not know them and prices those hours at the peak rate (an overestimate, never an underestimate).
        "peak_utc": [(time(1, 0), time(4, 0)), (time(6, 0), time(10, 0))],
        "peak_weekdays": (0, 1, 2, 3, 4),
        "models": {
            "deepseek-v4-pro": _DEEPSEEK_PRO,
            "deepseek-flash": _DEEPSEEK_FLASH,
            "deepseek-v4-flash": _DEEPSEEK_FLASH,  # retired name, billed at the Flash price
            "deepseek-v4-flash-vision-exp": _DEEPSEEK_FLASH,
        },
    },
]


def _entry(base_url: str | None) -> dict[str, Any] | None:
    host = (httpx.URL(base_url or "https://api.openai.com").host or "").lower()
    for e in CATALOG:
        if any(host == h or host.endswith("." + h) for h in e["hosts"]):
            return e
    return None


def is_peak(entry: dict[str, Any], at: datetime) -> bool:
    u = at.astimezone(timezone.utc)
    if u.weekday() not in entry["peak_weekdays"]:
        return False
    t = u.time()
    return any(a <= t < b for a, b in entry["peak_utc"])


def catalog_price(base_url: str | None, model: str, at: datetime | None = None) -> dict[str, Any] | None:
    """Rates for this provider and model at time `at` (now if omitted), or None when the catalog does not cover them."""
    e = _entry(base_url)
    if e is None or model not in e["models"]:
        return None
    at = at or datetime.now(timezone.utc)
    period = "peak" if is_peak(e, at) else "offpeak"
    r = e["models"][model][period]
    return {"in": r["in"], "out": r["out"], "cached_in": r["cached_in"], "version": 0, "catalog": CATALOG_VERSION,
            "period": period, "source": f"built-in {e['provider']} {period} rate ({CATALOG_VERSION}; {e['source']})"}


def catalog_rows(base_url: str | None) -> list[dict[str, Any]]:
    """For display: every model this provider has in the catalog, with both rates."""
    e = _entry(base_url)
    if e is None:
        return []
    return [{"model": m, "peak": r["peak"], "offpeak": r["offpeak"], "source": e["source"], "catalog": CATALOG_VERSION,
             "peak_hours_utc": [f"{a:%H:%M}-{b:%H:%M}" for a, b in e["peak_utc"]], "peak_days": "Mon-Fri"}
            for m, r in e["models"].items()]


def reprice_history(db) -> dict[str, Any] | None:
    """Once per catalog version: recompute the cost of past calls the catalog covers, from their recorded tokens and
    start times, so budgets and daily limits reflect what the provider actually charges. Seen live: prices entered
    without the off-peak and cache-hit rates overstated a run about six-fold ($0.84 counted, $0.13 charged).
    Calls recorded before cache hits were tracked are priced as if nothing was cached (still an overestimate)."""
    from ..persistence.db import dumps, loads, now_iso
    from ..persistence.events import emit
    from .base import Usage
    from .gateway import call_cost

    done = db.one("SELECT value_json FROM settings WHERE key = 'pricing.catalog_applied'")
    if done and loads(done["value_json"], None) == CATALOG_VERSION:
        return None
    rows = db.query("SELECT c.id, c.model, c.started_at, c.input_tokens, c.output_tokens, c.cached_input_tokens,"
                    " c.actual_usd, p.base_url FROM model_calls c JOIN providers p ON p.id = c.provider_id"
                    " WHERE p.kind = 'openai' AND c.status = 'succeeded' AND c.input_tokens IS NOT NULL")
    before = after = 0.0
    changed = 0
    with db.tx() as cur:
        for r in rows:
            at = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
            price = catalog_price(r["base_url"], r["model"], at)
            if price is None:
                continue
            cost = call_cost(Usage(r["input_tokens"], r["output_tokens"], r["cached_input_tokens"] or 0), price)
            before += r["actual_usd"] or 0
            after += cost
            if r["actual_usd"] is None or abs((r["actual_usd"] or 0) - cost) > 1e-12:
                cur.execute("UPDATE model_calls SET actual_usd = ? WHERE id = ?", (cost, r["id"]))
                cur.execute("UPDATE budget_reservations SET actual_usd = ? WHERE ref = ? AND status = 'reconciled'",
                            (cost, r["id"]))
                changed += 1
        cur.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?,?,?)",
                    ("pricing.catalog_applied", dumps(CATALOG_VERSION), now_iso()))
        report = {"catalog": CATALOG_VERSION, "calls_repriced": changed, "before_usd": round(before, 4),
                  "after_usd": round(after, 4)}
        emit(cur, "pricing.repriced", "nexus:pricing", report)
    return report
