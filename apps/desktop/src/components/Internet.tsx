import { useEffect, useState } from "react";
import { put, useApi } from "../api";
import { Badge, Button, Card, Field, Load } from "./ui";

export function InternetPanel({ projectId }: { projectId: string }) {
  const q = useApi<any>(`/api/projects/${projectId}/internet`, (e) => e.type === "internet.policy_changed");
  const search = useApi<any>("/api/search-provider", (e) => e.type === "settings.changed");
  const autonomy = useApi<any>(`/api/projects/${projectId}/autonomy`, (e) => e.type === "autonomy.changed");
  const unattended = autonomy.data?.mode === "unattended";
  const [form, setForm] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  const edit = (patch: any) => { setForm({ ...form, ...patch }); setDirty(true); setSaved(false); };
  const choose = (patch: any) => { const n = { ...form, ...patch }; setForm(n); void save(n); }; // modes save immediately
  useEffect(() => { if (q.data && !form) setForm({ ...q.data, blocked: q.data.blocked_domains.join(", "), lan: q.data.allowed_private_hosts.join(", ") }); }, [q.data]);

  async function save(next: any = form) {
    setErr(null); setSaved(false);
    const form = next;
    try {
      const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
      await put(`/api/projects/${projectId}/internet`, { mode: form.mode, write_autonomy: form.write_autonomy, contact: form.contact ?? "",
        blocked_domains: split(form.blocked), allowed_private_hosts: split(form.lan), max_requests_per_minute: Number(form.max_requests_per_minute),
        max_auto_writes_per_day: Number(form.max_auto_writes_per_day ?? 200) });
      setSaved(true); setDirty(false); q.reload();
    } catch (e) { setErr((e as Error).message); }
  }

  return (
    <Card title="Internet access (all agents)" id="inet">
      <Load q={q}>{(d) => !form ? null : (
        <div className="col">
          <div className="small">Every agent has {d.tools.length} built-in tools, including web search, page/PDF/JSON/RSS reading, downloads and public knowledge APIs. Web reads are recorded as citable evidence.</div>
          <div className="grid thirds">
            <Field label="Internet mode" htmlFor="im"><select id="im" value={form.mode} onChange={(e) => choose({ mode: e.target.value })}>
              <option value="read_write">Read and write (any public site)</option><option value="read">Read only</option><option value="off">Off (allowlist only)</option></select></Field>
            <Field label="Web writes (POST, forms)" htmlFor="iw" hint={unattended ? "Unattended mode: the Autonomy guard decides each write (this setting applies when supervised). Deletes always ask." : "Deletes always ask you."}><select id="iw" value={form.write_autonomy} disabled={form.mode !== "read_write" || unattended} onChange={(e) => choose({ write_autonomy: e.target.value })}>
              <option value="ask">Ask me each time</option><option value="auto">Pre-approved (no prompt)</option></select></Field>
            <Field label="Requests per minute" htmlFor="ir"><input id="ir" type="number" min={1} max={1000} value={form.max_requests_per_minute} onChange={(e) => edit({ max_requests_per_minute: e.target.value })} /></Field>
          </div>
          {form.mode === "read_write" && form.write_autonomy === "auto" && (
            <div className="callout warn small" role="alert">Pre-approved writes let agents post to any public website without asking you, up to {form.max_auto_writes_per_day ?? 200} per day. A page an agent reads can try to trick it, so pre-approval means you see these requests only after they are sent (Connected services → Action requests).</div>
          )}
          <div className="grid thirds">
            <Field label="Contact for the User-Agent" htmlFor="ic" hint="Email or URL. Some sites (e.g. Wikipedia) require it."><input id="ic" value={form.contact ?? ""} onChange={(e) => edit({ contact: e.target.value })} /></Field>
            <Field label="Blocked domains" htmlFor="ib" hint="Comma-separated; subdomains included."><input id="ib" value={form.blocked} onChange={(e) => edit({ blocked: e.target.value })} /></Field>
            <Field label="Allowed local-network hosts" htmlFor="il" hint="Local addresses are blocked unless listed."><input id="il" value={form.lan} onChange={(e) => edit({ lan: e.target.value })} /></Field>
          </div>
          <details className="small"><summary>Guards that stay on in every mode</summary><ul>{d.guards.map((g: string) => <li key={g}>{g}</li>)}</ul></details>
          <details className="small"><summary>Tools every agent has ({d.tools.length})</summary><div className="row wrap">{d.tools.map((t: string) => <Badge key={t}>{t}</Badge>)}</div></details>
          {err && <div className="callout bad" role="alert">{err}</div>}
          <div className="row"><Button variant="primary" onClick={() => save()}>Save internet settings</Button>{saved && <span className="small muted" role="status">Saved</span>}
            {dirty && <span className="small" style={{ color: "var(--warn)" }} role="status">Unsaved changes</span>}</div>
        </div>
      )}</Load>
      <SearchProvider q={search} />
    </Card>
  );
}

function SearchProvider({ q }: { q: any }) {
  const [kind, setKind] = useState<string>("duckduckgo");
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => { if (q.data) { setKind(q.data.kind); setUrl(q.data.base_url ?? ""); } }, [q.data]);
  return (
    <div className="col" style={{ borderTop: "1px solid var(--border)", marginTop: 12, paddingTop: 12 }}>
      <h4>Web search provider</h4>
      <div className="row wrap">
        <select aria-label="search provider" value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 220 }}>
          <option value="duckduckgo">DuckDuckGo (no key)</option><option value="brave">Brave Search API</option>
          <option value="tavily">Tavily</option><option value="searxng">SearXNG (your instance)</option></select>
        {kind === "searxng" && <input aria-label="searxng url" placeholder="https://search.example.org" value={url} onChange={(e) => setUrl(e.target.value)} style={{ width: 260 }} />}
        {(kind === "brave" || kind === "tavily") && <input aria-label="search api key" type="password" placeholder={q.data?.has_key ? "key stored (enter to replace)" : "API key"} value={key} onChange={(e) => setKey(e.target.value)} style={{ width: 240 }} />}
        <Button onClick={async () => { try { await put("/api/search-provider", { kind, base_url: url || null, api_key: key || null }); setKey(""); setMsg("Saved"); q.reload(); } catch (e) { setMsg((e as Error).message); } }}>Save</Button>
        {msg && <span className="small muted">{msg}</span>}
      </div>
      <div className="small faint">DuckDuckGo works without an account but parses its HTML results page, which can change; a keyed provider is more reliable.</div>
    </div>
  );
}
