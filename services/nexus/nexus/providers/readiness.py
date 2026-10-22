"""Live-mission readiness: every model an agent would use must be reachable and priced BEFORE a
live mission is created (otherwise the first call would block mid-mission)."""

from __future__ import annotations

from typing import Any

from ..persistence.db import Database, loads


def live_readiness(db: Database, registry, project_id: str) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    d = db.one("SELECT value_json FROM settings WHERE key = 'default_model_policy'")
    default = loads(d["value_json"], {}) if d else {}
    agents = db.query("SELECT a.name, v.role_default, v.model_policy_json FROM agent_identities a JOIN agent_versions v ON"
                      " v.id = a.current_version_id WHERE (a.project_id = ? OR a.project_id IS NULL) AND a.archived = 0",
                      (project_id,))
    uses: dict[tuple[str, str], list[str]] = {}
    for a in agents:
        pol = {**default, **{k: v for k, v in loads(a["model_policy_json"], {}).items() if v}}
        if not pol.get("provider_id") or not pol.get("model"):
            problems.append({"kind": "no_model", "agent": a["name"], "role": a["role_default"],
                             "fix": "Choose a default model (Settings > Default model) or one for this agent (Agent studio)."})
            continue
        uses.setdefault((pol["provider_id"], pol["model"]), []).append(a["name"])
    checked = {r["payload_json"] for r in db.query("SELECT payload_json FROM events WHERE type = 'provider.capability_checked'")}
    for (pid, model), names in uses.items():
        row = db.one("SELECT * FROM providers WHERE id = ?", (pid,))
        if row is None:
            problems.append({"kind": "provider_missing", "model": model, "agents": names, "fix": "Re-select a provider."})
            continue
        if row["kind"] == "fake":
            problems.append({"kind": "fake_provider", "model": model, "agents": names,
                             "fix": "Live missions need a real provider; the offline fake is for demos."})
            continue
        if row["status"] != "ok":
            problems.append({"kind": "provider_not_ok", "provider": row["label"], "status": row["status"], "agents": names,
                             "fix": "Test the provider connection in Settings > Providers."})
        if registry.price(pid, model) is None:
            problems.append({"kind": "no_price", "provider": row["label"], "provider_id": pid, "model": model, "agents": names,
                             "fix": f"Enter the price of {model} (USD per million input/output tokens) from the provider's"
                                    f" pricing page. NEXUS needs it to enforce your spend cap."})
        if not any(f'"model":"{model}"' in c and f'"provider_id":"{pid}"' in c and '"passed":true' in c for c in checked):
            warnings.append({"kind": "capability_unchecked", "provider": row["label"], "model": model,
                             "fix": "Run Check capabilities on this model (3 tiny calls) before relying on it."})
    return {"ready": not problems, "problems": problems, "warnings": warnings,
            "models": [{"provider_id": p, "model": m, "agents": n} for (p, m), n in uses.items()]}
