import { useState } from "react";
import { del, post, put, useApi } from "../api";
import { PriceFix } from "./PriceFix";
import { Badge, Button, Field, Load, Status } from "./ui";

const KINDS: { id: string; label: string; needsKey: boolean; url: string; kind?: string }[] = [
  { id: "anthropic", label: "Anthropic API", needsKey: true, url: "https://api.anthropic.com" },
  { id: "openai", label: "OpenAI API", needsKey: true, url: "https://api.openai.com" },
  { id: "deepseek", label: "DeepSeek API", needsKey: true, url: "https://api.deepseek.com", kind: "openai" },
  { id: "openai-compatible", label: "Other OpenAI-compatible (LM Studio, vLLM, ...)", needsKey: true, url: "http://127.0.0.1:1234", kind: "openai" },
  { id: "ollama", label: "Ollama (local or LAN)", needsKey: false, url: "http://127.0.0.1:11434" },
];

export function ProvidersPanel() {
  const q = useApi<any[]>("/api/providers", (e) => e.type.startsWith("provider.") || e.type === "pricing.set");
  const [kind, setKind] = useState("anthropic");
  const [label, setLabel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const k = KINDS.find((x) => x.id === kind)!;

  async function add() {
    setBusy(true);
    setErr(null);
    try {
      const p = await post("/api/providers", { kind: k.kind ?? kind, label: label || k.label,
        base_url: baseUrl || (k.kind ? k.url : null), api_key: k.needsKey ? key : null });
      setKey("");
      setLabel("");
      await post(`/api/providers/${p.id}/validate`);
      q.reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col gap-lg">
      <Load q={q}>{(rows) => (
        <div className="col">
          {rows.filter((r) => r.kind !== "fake").length === 0 && <div className="callout info">No real providers yet. Missions can still run offline with the deterministic fake (clearly labeled DEMO).</div>}
          {rows.map((p) => <ProviderRow key={p.id} p={p} reload={q.reload} />)}
        </div>
      )}</Load>
      <section className="card" aria-labelledby="add-provider">
        <div className="card-head"><h3 id="add-provider">Add a provider</h3></div>
        <div className="card-body col">
          <div className="callout warn small">An API key is billed by the provider separately from any consumer chat subscription.
            Keys are stored in the operating system credential store; NEXUS records only an opaque reference and never shows the key again.</div>
          <div className="grid thirds">
            <Field label="Provider" htmlFor="pk"><select id="pk" value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}</select></Field>
            <Field label="Label" htmlFor="pl"><input id="pl" value={label} placeholder={k.label} onChange={(e) => setLabel(e.target.value)} /></Field>
            <Field label="Base URL" htmlFor="pu" hint="Leave empty for the default."><input id="pu" value={baseUrl} placeholder={k.url} onChange={(e) => setBaseUrl(e.target.value)} /></Field>
          </div>
          {k.needsKey && (
            <Field label="API key" htmlFor="pkey"><input id="pkey" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} /></Field>
          )}
          {err && <div className="callout bad" role="alert">{err}</div>}
          <div><Button variant="primary" disabled={busy || (k.needsKey && key.length < 8)} onClick={add}>{busy ? "Saving and testing…" : "Save and test connection"}</Button></div>
          <div className="small muted">Testing lists available models (a free call). No completion is sent.</div>
        </div>
      </section>
    </div>
  );
}

function ProviderRow({ p, reload }: { p: any; reload: () => void }) {
  const [model, setModel] = useState("");
  const [pin, setPin] = useState("");
  const [pout, setPout] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [newKey, setNewKey] = useState("");
  const [checkModel, setCheckModel] = useState("");
  const [check, setCheck] = useState<any>(null);
  async function act(fn: () => Promise<unknown>) {
    setBusy(true); setErr(null);
    try { await fn(); reload(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <section className="card" data-testid={`provider-${p.kind}`}>
      <div className="card-head">
        <h3>{p.label}</h3>
        {p.kind === "fake" && <Badge tone="sim">offline simulation</Badge>}
        <Status value={p.status} />
        {p.sends_data_off_machine ? <Badge tone="warn" title="Prompts and context leave this computer">sends data off-machine</Badge>
          : <Badge tone="ok">stays on this machine</Badge>}
      </div>
      <div className="card-body col">
        {p.status_detail && <div className="small muted">{p.status_detail}</div>}
        {p.kind !== "fake" && (
          <>
            <div className="small">{p.models.length} model(s) discovered{p.validated_at ? ` · tested ${new Date(p.validated_at).toLocaleString()}` : ""}</div>
            <div className="btn-group">
              <Button size="sm" disabled={busy} onClick={() => act(() => post(`/api/providers/${p.id}/validate`))}>Test again</Button>
              {p.has_key !== undefined && p.kind !== "ollama" && (
                <>
                  <label className="sr-only" htmlFor={`rk-${p.id}`}>Replace key</label>
                  <input id={`rk-${p.id}`} type="password" placeholder="replace key" value={newKey} onChange={(e) => setNewKey(e.target.value)} style={{ width: 200 }} />
                  <Button size="sm" disabled={busy || newKey.length < 8} onClick={() => act(async () => { await put(`/api/providers/${p.id}/key`, { api_key: newKey }); setNewKey(""); await post(`/api/providers/${p.id}/validate`); })}>Replace key</Button>
                </>
              )}
              <Button size="sm" variant="danger" disabled={busy} onClick={() => confirm(`Remove ${p.label}? Its key is deleted from the credential store.`) && act(() => del(`/api/providers/${p.id}`))}>Remove</Button>
            </div>
            <h4>Capability check</h4>
            <div className="small muted">Makes 3 tiny real calls (structured JSON, a tool call and its result). Billed by the provider; typically well under a cent.</div>
            <div className="row wrap">
              <select aria-label={`model to check for ${p.label}`} value={checkModel} onChange={(e) => setCheckModel(e.target.value)} style={{ width: 260 }}>
                <option value="">Choose a model…</option>{p.models.map((m: string) => <option key={m} value={m}>{m}</option>)}</select>
              <Button size="sm" disabled={busy || !checkModel} onClick={() => act(async () => { setCheck(null); setCheck(await post(`/api/providers/${p.id}/check`, { value: checkModel })); })}>Check capabilities</Button>
            </div>
            {check && (
              <div className={`callout ${check.passed ? "ok" : "bad"} small`} data-testid="capability-result">
                <strong>{check.passed ? "Ready for NEXUS" : "Not fully compatible"}</strong> · {check.model} · {check.calls} calls{check.estimated_cost_usd ? ` · ~$${check.estimated_cost_usd}` : ""}
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{check.results.map((r: any) => <li key={r.check}>{r.passed ? "✓" : "✗"} {r.check.replace(/_/g, " ")} — {r.detail}</li>)}</ul>
              </div>
            )}
            <h4>Prices (required for paid execution)</h4>
            {p.prices.length === 0 ? <div className="small muted">No prices configured. Live missions using this provider are refused until a price is set for the model they use. Provider APIs do not report prices; enter the provider's published rate.</div> : (
              <table className="data"><thead><tr><th>Model</th><th>Input $/Mtok</th><th>Output $/Mtok</th><th>Source</th><th>v</th></tr></thead>
                <tbody>{p.prices.map((x: any) => <tr key={`${x.model}${x.version}`}><td className="mono">{x.model}</td><td>{x.input_per_mtok_usd}</td><td>{x.output_per_mtok_usd}</td><td className="small">{x.source}</td><td>{x.version}</td></tr>)}</tbody></table>)}
            <div className="row wrap">
              <label className="sr-only" htmlFor={`pm-${p.id}`}>Model</label>
              <select id={`pm-${p.id}`} value={model} onChange={(e) => setModel(e.target.value)} style={{ width: 260 }}>
                <option value="">Choose a discovered model…</option>
                {p.models.map((m: string) => <option key={m} value={m}>{m}</option>)}
              </select>
              <input aria-label="input price per million tokens" type="number" min="0" step="0.01" placeholder="input $/Mtok" value={pin} onChange={(e) => setPin(e.target.value)} style={{ width: 120 }} />
              <input aria-label="output price per million tokens" type="number" min="0" step="0.01" placeholder="output $/Mtok" value={pout} onChange={(e) => setPout(e.target.value)} style={{ width: 120 }} />
              <Button size="sm" disabled={busy || !model || pin === "" || pout === ""}
                      onClick={() => act(() => post(`/api/providers/${p.id}/prices`, { model, input_per_mtok_usd: Number(pin), output_per_mtok_usd: Number(pout), source: `owner-configured ${new Date().toISOString().slice(0, 10)}` }))}>Save price</Button>
            </div>
          </>
        )}
        {err && <div className="callout bad" role="alert">{err}</div>}
      </div>
    </section>
  );
}

export function DefaultModelPicker() {
  const providers = useApi<any[]>("/api/providers", (e) => e.type.startsWith("provider."));
  const settings = useApi<any>("/api/settings", (e) => e.type === "settings.changed");
  const [saved, setSaved] = useState<string | null>(null);
  return (
    <Load q={providers}>{(rows) => {
      const usable = rows.filter((p) => p.kind !== "fake" && p.status === "ok");
      const cur = settings.data?.default_model_policy ?? {};
      if (!usable.length) return <div className="callout warn">No validated real provider. Live missions cannot run until one is added and tested; offline demo missions still work.</div>;
      return (
        <div className="row wrap">
          <select aria-label="Default provider" value={cur.provider_id ?? ""} onChange={async (e) => {
            await put("/api/settings/default_model_policy", { value: { ...cur, provider_id: e.target.value, model: null } }); settings.reload(); }} style={{ width: 240 }}>
            <option value="">Choose provider…</option>
            {usable.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          <select aria-label="Default model" value={cur.model ?? ""} disabled={!cur.provider_id} onChange={async (e) => {
            await put("/api/settings/default_model_policy", { value: { ...cur, model: e.target.value } }); settings.reload(); setSaved("Saved"); }} style={{ width: 300 }}>
            <option value="">Choose model…</option>
            {(usable.find((p) => p.id === cur.provider_id)?.models ?? []).map((m: string) => <option key={m} value={m}>{m}</option>)}
          </select>
          {saved && <span className="small muted" role="status">{saved}</span>}
          {(() => {
            const prov = usable.find((p) => p.id === cur.provider_id);
            if (!prov || !cur.model || prov.prices.some((x: any) => x.model === cur.model)) return null;
            return (
              <div className="callout warn small" style={{ flexBasis: "100%" }}>
                <div><strong>{cur.model}</strong> has no price yet. Live missions need it to enforce your spend cap.</div>
                <PriceFix providerId={prov.id} model={cur.model} onSaved={() => providers.reload()} />
              </div>
            );
          })()}
          <div className="small muted" style={{ flexBasis: "100%" }}>Used by every agent without its own model policy. Models come from the provider's live model list; NEXUS does not assume a "current flagship".</div>
        </div>
      );
    }}</Load>
  );
}
