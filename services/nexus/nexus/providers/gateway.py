"""Provider registry and the model gateway: the only path to a model.

Every call: resolve provider -> enforce mode/data-exposure policy -> price -> atomic budget
reservation -> provider call -> usage reconciliation -> immutable output record.
Calls are keyed by a stable operation id; a replay of the same operation reuses the recorded
output (debug replay) instead of calling the model again.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
import json
import re
from dataclasses import dataclass
from typing import Any

from ..domain.budget import BudgetExceeded, reconcile, reserve
from ..persistence.artifacts import ArtifactStore
from ..persistence.db import Database, dumps, loads, new_id, now_iso
from ..persistence.events import emit
from ..secrets import SecretStore
from .base import (
    REJECTED_UNBILLED,
    CompletionRequest,
    CompletionResult,
    ModelInfo,
    ProviderError,
    estimate_input_tokens_upper,
)
from .http_adapters import AnthropicAdapter, OllamaAdapter, OpenAIAdapter

FREE_KINDS = {"fake", "ollama"}  # local compute; owner may still configure a price


class ProviderRegistry:
    def __init__(self, db: Database, secrets: SecretStore):
        self.db = db
        self.secrets = secrets
        self._adapters: dict[str, Any] = {}
        self._overrides: dict[str, Any] = {}  # in-process adapters (fake, test transports)

    # ---- configuration ---------------------------------------------------------------------
    def add(self, *, kind: str, label: str, base_url: str | None = None, api_key: str | None = None) -> str:
        if kind not in ("anthropic", "openai", "ollama", "fake"):
            raise ValueError(f"unknown provider kind {kind}")
        if kind in ("anthropic", "openai") and not api_key:
            raise ValueError(f"{kind} requires an API key")
        ref = self.secrets.put(api_key) if api_key else None
        pid = new_id("prv")
        with self.db.tx() as cur:
            cur.execute("INSERT INTO providers (id, kind, label, base_url, secret_ref, created_at) VALUES (?,?,?,?,?,?)",
                        (pid, kind, label, base_url, ref, now_iso()))
            emit(cur, "provider.added", "owner", {"provider_id": pid, "kind": kind, "label": label,
                                                  "has_key": bool(api_key)})
        return pid

    def replace_key(self, provider_id: str, api_key: str) -> None:
        row = self.get_row(provider_id)
        new_ref = self.secrets.put(api_key)
        with self.db.tx() as cur:
            cur.execute("UPDATE providers SET secret_ref = ?, status = 'unvalidated', status_detail = NULL WHERE id = ?",
                        (new_ref, provider_id))
            emit(cur, "provider.key_replaced", "owner", {"provider_id": provider_id})
        self.secrets.delete(row["secret_ref"])
        self._adapters.pop(provider_id, None)

    def remove(self, provider_id: str) -> None:
        row = self.get_row(provider_id)
        with self.db.tx() as cur:
            cur.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            emit(cur, "provider.removed", "owner", {"provider_id": provider_id})
        self.secrets.delete(row["secret_ref"])
        self._adapters.pop(provider_id, None)

    def register_override(self, provider_id: str, adapter: Any) -> None:
        self._overrides[provider_id] = adapter
        self._adapters.pop(provider_id, None)

    def get_row(self, provider_id: str):
        r = self.db.one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        if r is None:
            raise ProviderError("not_configured", f"provider {provider_id} not found")
        return r

    def public_view(self, r) -> dict[str, Any]:
        return {"id": r["id"], "kind": r["kind"], "label": r["label"], "base_url": r["base_url"],
                "has_key": bool(r["secret_ref"]), "status": r["status"], "status_detail": r["status_detail"],
                "validated_at": r["validated_at"], "models": loads(r["models_json"], []),
                "sends_data_off_machine": self._sends_off_machine(r)}

    def _sends_off_machine(self, r) -> bool:
        if r["kind"] == "fake":
            return False
        if r["kind"] == "ollama":
            return OllamaAdapter(r["base_url"] or "http://127.0.0.1:11434").sends_data_off_machine
        return True

    def adapter(self, provider_id: str):
        if provider_id in self._overrides:
            return self._overrides[provider_id]
        if provider_id in self._adapters:
            return self._adapters[provider_id]
        r = self.get_row(provider_id)
        if r["kind"] == "fake":
            raise ProviderError("not_configured", "fake provider has no in-process script registered")
        key = self.secrets.get(r["secret_ref"])
        if r["kind"] == "anthropic":
            if not key:
                raise ProviderError("not_configured", "Anthropic API key missing from credential store")
            a = AnthropicAdapter(key, r["base_url"] or "https://api.anthropic.com")
        elif r["kind"] == "openai":
            if not key:
                raise ProviderError("not_configured", "OpenAI API key missing from credential store")
            a = OpenAIAdapter(key, r["base_url"] or "https://api.openai.com")
        else:
            a = OllamaAdapter(r["base_url"] or "http://127.0.0.1:11434")
        self._adapters[provider_id] = a
        return a

    async def validate(self, provider_id: str) -> dict[str, Any]:
        """Lists models (a free call on all three providers) to verify reachability + credentials."""
        status, detail, models = "ok", None, []
        try:
            models = await self.adapter(provider_id).list_models()
            if not models:
                status, detail = "no_models", "provider reachable but returned no models"
        except ProviderError as exc:
            status, detail = {"auth": "auth_failed", "network": "unreachable", "timeout": "unreachable"}.get(
                exc.kind, "error"), exc.detail
        with self.db.tx() as cur:
            cur.execute("UPDATE providers SET status = ?, status_detail = ?, validated_at = ?, models_json = ? WHERE id = ?",
                        (status, detail, now_iso(), dumps([m.id for m in models]), provider_id))
            emit(cur, "provider.validated", "nexus:providers", {"provider_id": provider_id, "status": status,
                                                                 "detail": detail, "model_count": len(models)})
        return {"status": status, "detail": detail, "models": [m.id for m in models]}

    # ---- pricing ---------------------------------------------------------------------------
    def set_price(self, provider_id: str, model: str, input_per_mtok: float, output_per_mtok: float, source: str,
                  cached_input_per_mtok: float | None = None) -> int:
        if input_per_mtok < 0 or output_per_mtok < 0 or (cached_input_per_mtok is not None and cached_input_per_mtok < 0):
            raise ValueError("prices must be non-negative")
        with self.db.tx() as cur:
            v = cur.execute("SELECT COALESCE(MAX(version),0) AS v FROM model_prices WHERE provider_id=? AND model=?",
                            (provider_id, model)).fetchone()["v"] + 1
            cur.execute("INSERT INTO model_prices (id, provider_id, model, input_per_mtok_usd, output_per_mtok_usd, source,"
                        " version, created_at, cached_input_per_mtok_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                        (new_id("prc"), provider_id, model, input_per_mtok, output_per_mtok, source, v, now_iso(),
                         cached_input_per_mtok))
            emit(cur, "pricing.set", "owner", {"provider_id": provider_id, "model": model, "version": v,
                                               "input_per_mtok_usd": input_per_mtok, "output_per_mtok_usd": output_per_mtok,
                                               "cached_input_per_mtok_usd": cached_input_per_mtok})
        return v

    def price(self, provider_id: str, model: str, at: datetime | None = None) -> dict[str, Any] | None:
        """Rates for a call starting at `at` (now if omitted). The built-in catalog wins for the models it covers
        (official rates, including time-of-day pricing); owner-entered prices cover everything else."""
        from .price_catalog import catalog_price

        row = self.get_row(provider_id)
        if row["kind"] == "openai":
            cp = catalog_price(row["base_url"], model, at)
            if cp is not None:
                return cp
        r = self.db.one("SELECT * FROM model_prices WHERE provider_id=? AND model=? ORDER BY version DESC LIMIT 1",
                        (provider_id, model))
        if r is not None:
            return {"in": r["input_per_mtok_usd"], "out": r["output_per_mtok_usd"], "version": r["version"],
                    "source": r["source"], "cached_in": r["cached_input_per_mtok_usd"]}
        kind = self.get_row(provider_id)["kind"]
        if kind in FREE_KINDS:
            return {"in": 0.0, "out": 0.0, "version": 0, "source": f"default: {kind} runs locally (no API charge)"}
        return None


@dataclass
class CallOutcome:
    result: CompletionResult
    model_call_id: str
    replayed: bool
    actual_usd: float | None


TRUNCATED = {"length", "max_tokens"}  # openai/deepseek/ollama, anthropic
OUTPUT_CEILING = 16384
RETRY_MARK = ":more"  # operation-id suffix of a truncation retry, followed by the limit it used
RETRY_LIMIT_RE = re.compile(r":more(\d+)$")



def call_cost(usage, price: dict[str, Any]) -> float:
    """Cost from provider-reported tokens. Cached input (reported by the provider) is billed at the cached rate when
    the owner configured one; otherwise at the normal input rate, which can only overstate the cost."""
    cached = min(max(int(getattr(usage, "cached_input_tokens", 0) or 0), 0), usage.input_tokens)
    cached_rate = price.get("cached_in")
    if cached_rate is None:
        cached_rate = price["in"]
    return ((usage.input_tokens - cached) * price["in"] + cached * cached_rate + usage.output_tokens * price["out"]) / 1_000_000


class ModelGateway:
    def __init__(self, db: Database, registry: ProviderRegistry, artifacts: ArtifactStore):
        self.db = db
        self.registry = registry
        self.artifacts = artifacts
        self.fault_hook = None  # test-only crash injection

    def _mode_check(self, mission: Any, provider_row: Any) -> None:
        if mission is None:
            return
        if mission["execution_mode"] == "offline_fake" and provider_row["kind"] != "fake":
            raise ProviderError("not_configured", "offline mission may only use the deterministic fake provider")
        if mission["execution_mode"] == "live" and provider_row["kind"] == "fake":
            raise ProviderError("not_configured",
                                "live mission has no real provider configured for this agent (fake substitution refused)")

    async def call(self, req: CompletionRequest, *, operation_id: str, **kw) -> CallOutcome:
        """One model call. Reasoning models (DeepSeek, o-series, extended thinking) spend output tokens on hidden
        reasoning; when that exhausts the limit the reply is empty or truncated. Then the call is repeated once,
        under its own operation id and budget reservation, with a larger limit (OUTPUT_CEILING at most).
        Such a retry is remembered: later calls for the same provider, model and purpose start at the limit that
        worked, so the truncated first call (which is still billed) is not paid for again on every mission.
        A higher limit only enlarges the budget reservation; providers bill the tokens actually produced."""
        learned = self.learned_output_limit(kw.get("provider_id"), req.model, kw.get("purpose"))
        if learned and req.max_output_tokens < learned:
            req = dataclasses.replace(req, max_output_tokens=learned)
        out = await self._call_once(req, operation_id=operation_id, **kw)
        r = out.result
        if (r.stop_reason in TRUNCATED and not r.tool_calls and req.max_output_tokens < OUTPUT_CEILING
                and (not r.text.strip() or req.response_json)):
            bigger = min(max(req.max_output_tokens * 4, 8192), OUTPUT_CEILING)
            return await self._call_once(dataclasses.replace(req, max_output_tokens=bigger),
                                         operation_id=f"{operation_id}{RETRY_MARK}{bigger}", **kw)
        return out

    def learned_output_limit(self, provider_id: str | None, model: str, purpose: str | None) -> int | None:
        """The largest output limit a truncation retry needed and succeeded with for this provider, model and
        purpose (read from recorded calls, so it survives restarts and covers calls made before this existed)."""
        if not provider_id or not purpose:
            return None
        rows = self.db.query("SELECT operation_id FROM model_calls WHERE provider_id = ? AND model = ? AND purpose = ?"
                             " AND status = 'succeeded' AND operation_id LIKE ?",
                             (provider_id, model, purpose, f"%{RETRY_MARK}%"))
        limits = [int(m.group(1)) for r in rows if (m := RETRY_LIMIT_RE.search(r["operation_id"]))]
        return min(max(limits), OUTPUT_CEILING) if limits else None

    async def _call_once(
        self,
        req: CompletionRequest,
        *,
        operation_id: str,
        provider_id: str,
        purpose: str,
        category: str = "model",
        project_id: str | None = None,
        mission_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        agent_id: str | None = None,
        context_artifact_id: str | None = None,
        timeout_s: float = 120.0,
        allow_unpriced: bool = False,
    ) -> CallOutcome:
        prior = self.db.one("SELECT * FROM model_calls WHERE operation_id = ?", (operation_id,))
        if prior is not None and prior["status"] == "succeeded" and prior["output_artifact_id"]:
            rec = json.loads(self.artifacts.read_text(prior["output_artifact_id"]))
            return CallOutcome(CompletionResult.from_record(rec), prior["id"], True, prior["actual_usd"])
        if prior is not None:
            raise ProviderError("duplicate_operation", f"operation {operation_id} already used (status"
                                                       f" {prior['status']}); retries need a new operation id")
        prow = self.registry.get_row(provider_id)
        mission = self.db.one("SELECT * FROM missions WHERE id = ?", (mission_id,)) if mission_id else None
        self._mode_check(mission, prow)
        price = self.registry.price(provider_id, req.model)
        if price is None and allow_unpriced and not mission_id:
            price = {"in": 0.0, "out": 0.0, "version": 0, "source": "unpriced (cost unknown)", "unpriced": True}
        if price is None:
            raise ProviderError("not_configured",
                                f"no price configured for {prow['kind']}:{req.model}; set one in Settings > Providers"
                                " before paid execution")
        est_in = estimate_input_tokens_upper(req)
        est_usd = (est_in * price["in"] + req.max_output_tokens * price["out"]) / 1_000_000
        basis = (f"<= {est_in} input tok (chars/3 estimate) x ${price['in']}/Mtok + {req.max_output_tokens} max output tok"
                 f" x ${price['out']}/Mtok (price v{price['version']}: {price['source']})")
        call_id = new_id("mc")
        with self.db.tx() as cur:
            res_id = None
            if mission_id:
                res_id = reserve(cur, mission_id, category=category, amount_usd=est_usd, basis=basis, ref=call_id)
            cur.execute(
                "INSERT INTO model_calls (id, operation_id, project_id, mission_id, task_id, attempt_id, agent_id,"
                " provider_id, provider_kind, model, purpose, status, reservation_id, context_artifact_id, started_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, operation_id, project_id, mission_id, task_id, attempt_id, agent_id, provider_id, prow["kind"],
                 req.model, purpose, "running", res_id, context_artifact_id, now_iso()),
            )
            emit(cur, "model_call.started", agent_id or "nexus:gateway",
                 {"model_call_id": call_id, "provider": prow["kind"], "model": req.model, "purpose": purpose,
                  "reserved_usd": round(est_usd, 6), "sends_data_off_machine": self.registry._sends_off_machine(prow)},
                 project_id=project_id, mission_id=mission_id, task_id=task_id)
        try:
            if self.fault_hook:
                self.fault_hook("during_model_call")
            adapter = self.registry.adapter(provider_id)
            result = await adapter.complete(req, timeout_s=timeout_s)
        except asyncio.CancelledError:
            self._finish_error(call_id, res_id, "canceled", "canceled locally; provider may still bill", mission_id,
                               task_id, project_id, agent_id)
            raise
        except ProviderError as exc:
            self._finish_error(call_id, res_id, exc.kind, exc.detail, mission_id, task_id, project_id, agent_id)
            raise
        except Exception as exc:  # unknown adapter failure: treat as uncertain
            self._finish_error(call_id, res_id, "network", f"{type(exc).__name__}", mission_id, task_id, project_id,
                               agent_id)
            raise ProviderError("network", f"adapter failure {type(exc).__name__}") from None

        actual = None
        if result.usage is not None and not price.get("unpriced"):
            actual = call_cost(result.usage, price)
        with self.db.tx() as cur:
            out = self.artifacts.create(cur, project_id=project_id or "", kind="model_output",
                                        title=f"{purpose} output", content=dumps(result.to_record()),
                                        media_type="application/json", created_by=agent_id or "nexus:gateway",
                                        mission_id=mission_id, task_id=task_id) if project_id else None
            if res_id:
                if result.usage is None:
                    reconcile(cur, res_id, actual_usd=None, status="unresolved")
                else:
                    reconcile(cur, res_id, actual_usd=actual, status="reconciled")
            cur.execute(
                "UPDATE model_calls SET status='succeeded', input_tokens=?, output_tokens=?, cached_input_tokens=?,"
                " usage_reported=?, actual_usd=?,"
                " output_artifact_id=?, ended_at=? WHERE id=?",
                (result.usage.input_tokens if result.usage else None, result.usage.output_tokens if result.usage else None,
                 result.usage.cached_input_tokens if result.usage else None,
                 int(result.usage is not None), actual, out["id"] if out else None, now_iso(), call_id),
            )
            emit(cur, "model_call.finished", agent_id or "nexus:gateway",
                 {"model_call_id": call_id, "status": "succeeded", "actual_usd": actual,
                  "usage_reported": result.usage is not None,
                  "note": None if result.usage else "provider reported no usage; reservation kept as unresolved"},
                 project_id=project_id, mission_id=mission_id, task_id=task_id)
        return CallOutcome(result, call_id, False, actual)

    def _finish_error(self, call_id, res_id, kind, detail, mission_id, task_id, project_id, agent_id) -> None:
        unbilled = kind in REJECTED_UNBILLED
        status = "failed" if unbilled else ("canceled" if kind == "canceled" else "uncertain")
        with self.db.tx() as cur:
            if res_id:
                reconcile(cur, res_id, actual_usd=0.0 if unbilled else None,
                          status="released" if unbilled else "unresolved")
            cur.execute("UPDATE model_calls SET status=?, error_kind=?, error_detail=?, ended_at=? WHERE id=?",
                        (status, kind, detail[:500], now_iso(), call_id))
            emit(cur, "model_call.finished", agent_id or "nexus:gateway",
                 {"model_call_id": call_id, "status": status, "error_kind": kind, "detail": detail[:300],
                  "billing": "not billed (request rejected)" if unbilled else "unknown; reservation kept unresolved"},
                 project_id=project_id, mission_id=mission_id, task_id=task_id)


def model_info_list(models: list[ModelInfo]) -> list[str]:
    return [m.id for m in models]
