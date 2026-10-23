"""Provider balance snapshots: what the provider says it actually deducted, next to NEXUS's computed spend.

DeepSeek exposes GET /user/balance. Each snapshot is stored; the "deducted" amount is the sum of the decreases between
consecutive snapshots (an increase is a top-up and is ignored), compared with the spend NEXUS computed for calls made
through that provider over the same period. Calls made with the same key outside NEXUS also lower the balance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..persistence.db import dumps, new_id, now_iso

log = logging.getLogger("nexus.balance")
SNAPSHOT_EVERY_S = 600


def supports_balance(row) -> bool:
    if row is None or row["kind"] != "openai":
        return False
    host = (httpx.URL(row["base_url"] or "https://api.openai.com").host or "").lower()
    return host == "deepseek.com" or host.endswith(".deepseek.com")


async def snapshot(ctx, provider_id: str) -> dict[str, Any]:
    row = ctx.registry.get_row(provider_id)
    if not supports_balance(row):
        raise ValueError("this provider does not report an account balance")
    adapter = ctx.registry.adapter(provider_id)
    data = await adapter._request("GET", "/user/balance", timeout_s=20)
    infos = data.get("balance_infos") or []
    info = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else None)
    if info is None:
        raise ValueError("provider returned no balance")
    snap = {"id": new_id("bal"), "currency": str(info.get("currency")), "total_balance": float(info.get("total_balance")),
            "taken_at": now_iso()}
    with ctx.db.tx() as cur:
        cur.execute("INSERT INTO provider_balances (id, provider_id, currency, total_balance, raw_json, taken_at)"
                    " VALUES (?,?,?,?,?,?)", (snap["id"], provider_id, snap["currency"], snap["total_balance"],
                                              dumps(data), snap["taken_at"]))
    return snap


def comparison(db, provider_id: str) -> dict[str, Any]:
    snaps = db.query("SELECT currency, total_balance, taken_at FROM provider_balances WHERE provider_id = ?"
                     " ORDER BY taken_at", (provider_id,))
    if not snaps:
        return {"snapshots": 0}
    latest, first = snaps[-1], snaps[0]
    same = [s for s in snaps if s["currency"] == latest["currency"]]
    deducted, topups = 0.0, 0.0
    for a, b in zip(same, same[1:]):
        d = a["total_balance"] - b["total_balance"]
        if d >= 0:
            deducted += d
        else:
            topups -= d
    computed = db.one("SELECT COALESCE(SUM(actual_usd), 0) AS s, COUNT(*) AS n FROM model_calls WHERE provider_id = ?"
                      " AND started_at > ? AND started_at <= ?", (provider_id, same[0]["taken_at"], latest["taken_at"]))
    return {"snapshots": len(snaps), "currency": latest["currency"], "balance": latest["total_balance"],
            "as_of": latest["taken_at"], "since": same[0]["taken_at"], "deducted": round(deducted, 4),
            "topups": round(topups, 4), "nexus_computed_usd": round(float(computed["s"] or 0), 4),
            "calls": computed["n"], "comparable": latest["currency"] == "USD",
            "note": "Deducted = decreases between balance checks; top-ups ignored. Use of the same key outside NEXUS"
                    " also counts. Charges can land a little after a call."}


class BalanceWatcher:
    """Takes a snapshot for every provider that reports a balance, every SNAPSHOT_EVERY_S seconds."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._runner: asyncio.Task | None = None

    async def run(self) -> None:
        while True:
            for row in self.ctx.db.query("SELECT * FROM providers"):
                if supports_balance(row) and row["status"] == "ok":
                    try:
                        await snapshot(self.ctx, row["id"])
                    except Exception as exc:  # never let this disturb the service
                        log.info("balance check for %s failed: %s", row["label"], type(exc).__name__)
            await asyncio.sleep(SNAPSHOT_EVERY_S)

    def start(self) -> None:
        self._runner = asyncio.create_task(self.run(), name="balance-watcher")

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
