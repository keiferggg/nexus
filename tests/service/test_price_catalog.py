"""Built-in prices: official rates with DeepSeek's peak/off-peak schedule, preferred over owner-entered prices, and
past calls repriced once per catalog version.

Seen live: the owner entered DeepSeek's peak rates without the cache-hit rate; an off-peak run with 76% cache hits was
counted at $0.84 while DeepSeek charged $0.13."""
from datetime import datetime, timezone

import pytest

from nexus.appctx import build_context
from nexus.persistence.db import new_id, now_iso
from nexus.providers.base import Usage
from nexus.providers.gateway import call_cost
from nexus.providers.price_catalog import CATALOG_VERSION, catalog_price, reprice_history
from nexus.secrets import MemorySecretStore
from nexus.tools.sandbox import UnavailableSandbox

DS = "https://api.deepseek.com"
UTC = timezone.utc


def test_peak_and_off_peak_follow_the_published_schedule():
    fri = datetime(2026, 10, 23, tzinfo=UTC)  # a Friday
    at = lambda d, h, m=0: d.replace(hour=h, minute=m)
    assert catalog_price(DS, "deepseek-v4-pro", at(fri, 1, 0))["period"] == "peak"
    assert catalog_price(DS, "deepseek-v4-pro", at(fri, 3, 59))["period"] == "peak"
    assert catalog_price(DS, "deepseek-v4-pro", at(fri, 4, 0))["period"] == "offpeak"
    assert catalog_price(DS, "deepseek-v4-pro", at(fri, 9, 30))["period"] == "peak"
    assert catalog_price(DS, "deepseek-v4-pro", at(fri, 18, 27))["period"] == "offpeak"
    sat = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)
    assert catalog_price(DS, "deepseek-v4-pro", sat)["period"] == "offpeak", "weekends are off-peak"
    assert catalog_price(DS, "deepseek-flash", at(fri, 2))["in"] == 0.30
    assert catalog_price("https://api.openai.com", "gpt-x") is None
    assert catalog_price(DS, "some-unlisted-model") is None


def test_the_run_seen_live_costs_what_deepseek_charged():
    p = catalog_price(DS, "deepseek-v4-pro", datetime(2026, 10, 23, 18, 27, tzinfo=UTC))
    miss, hit, out = 144_768, 458_752, 11_564
    cost = call_cost(Usage(miss + hit, out, hit), p)
    assert round(cost, 4) == 0.1285  # the balance fell by $0.13


@pytest.fixture
def ctx(tmp_path):
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    yield c
    c.close()


def test_catalog_beats_owner_prices_and_history_is_repriced_once(ctx):
    pid = ctx.registry.add(kind="openai", label="DeepSeek", base_url=DS, api_key="sk-x-123456")
    ctx.registry.set_price(pid, "deepseek-v4-pro", 9.0, 9.0, "owner typed something wrong")
    assert ctx.registry.price(pid, "deepseek-v4-pro")["source"].startswith("built-in DeepSeek")
    ctx.registry.set_price(pid, "my-finetune", 2.0, 4.0, "owner")
    assert ctx.registry.price(pid, "my-finetune")["in"] == 2.0, "models the catalog does not list keep owner prices"
    cid = new_id("mc")
    with ctx.db.tx() as cur:
        cur.execute("INSERT INTO model_calls (id, operation_id, provider_id, provider_kind, model, purpose, status,"
                    " input_tokens, output_tokens, cached_input_tokens, actual_usd, started_at) VALUES"
                    " (?,?,?,?,?,?,?,?,?,?,?,?)", (cid, "op1", pid, "openai", "deepseek-v4-pro", "execute", "succeeded",
                                                    603_520, 11_564, 458_752, 0.842, "2026-10-23T18:27:51.974+00:00"))
    rep = reprice_history(ctx.db)
    assert rep["calls_repriced"] == 1 and rep["after_usd"] == 0.1285 and rep["before_usd"] == 0.842
    assert round(ctx.db.one("SELECT actual_usd FROM model_calls WHERE id = ?", (cid,))["actual_usd"], 4) == 0.1285
    assert reprice_history(ctx.db) is None, "once per catalog version"
    assert CATALOG_VERSION in ctx.db.one("SELECT value_json FROM settings WHERE key = 'pricing.catalog_applied'")["value_json"]
