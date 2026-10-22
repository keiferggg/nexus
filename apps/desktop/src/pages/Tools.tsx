import { post, put, useApi } from "../api";
import { Badge, Button, Card, Load, Mono, Status } from "../components/ui";

export default function Tools({ project }: { project: any }) {
  const q = useApi<any[]>(`/api/projects/${project.id}/tools`, (e) => e.type.startsWith("tool."));
  const settings = useApi<any>("/api/settings", (e) => e.type === "settings.changed");
  const pol = settings.data?.["tools.policy"] ?? { auto_activate_readonly_composites: false };
  return (
    <div className="page">
      <div className="page-head"><div className="titles"><h1>Tool workshop</h1>
        <div className="sub">Tools your agents created. A tool is usable only after its own tests pass and you activate it. Composite tools run each step under the calling agent's permissions; Python tools run only in the sandbox.</div></div></div>
      <Card title="Activation policy" id="tp">
        <label className="row small"><input type="checkbox" checked={!!pol.auto_activate_readonly_composites}
          onChange={async (e) => { await put("/api/settings/tools.policy", { value: { ...pol, auto_activate_readonly_composites: e.target.checked } }); settings.reload(); }} />
          Auto-activate read-only composite tools whose tests pass (writes and code always need you)</label>
      </Card>
      <div style={{ height: 12 }} />
      <Load q={q} empty={(d) => (d.length ? null : "No agent-created tools yet")}>{(rows) => (
        <div className="grid halves">{rows.map((t) => (
          <section key={t.id} className="card" data-testid={`tool-${t.name}`}>
            <div className="card-head"><h3><Mono>{t.name}</Mono> v{t.version}</h3><Badge>{t.kind}</Badge>
              <Badge tone={t.effect_class === "read_only" ? "neutral" : "warn"}>{t.effect_class.replace("_", " ")}</Badge><Status value={t.status} /></div>
            <div className="card-body col">
              <div className="small">{t.description}</div>
              <div className="small faint">by <span className="mono">{t.created_by}</span>{t.approved_by ? ` · activated by ${t.approved_by}` : ""}</div>
              {t.status_detail && <div className="callout warn small">{t.status_detail}</div>}
              <details><summary className="small">Spec</summary><pre className="pre">{JSON.stringify(t.spec, null, 1)}</pre></details>
              <div className="small"><strong>Tests:</strong> {t.test_report?.total ? `${t.test_report.passed}/${t.test_report.total} passed` : "not run"}
                {t.test_report?.note && <span className="faint"> · {t.test_report.note}</span>}</div>
              {t.test_report?.cases?.filter((c: any) => !c.passed).map((c: any) => <div key={c.case} className="small" style={{ color: "var(--bad)" }}>case {c.case}: {c.detail}</div>)}
              <div className="btn-group">
                {t.status === "tested" && <Button size="sm" variant="primary" onClick={async () => { await post(`/api/tools/${t.id}/approve`); q.reload(); }}>Activate</Button>}
                {["candidate", "test_failed", "tested"].includes(t.status) && <Button size="sm" onClick={async () => { await post(`/api/tools/${t.id}/retest`); q.reload(); }}>Re-run tests</Button>}
                {t.status === "active" && <Button size="sm" variant="danger" onClick={async () => { await post(`/api/tools/${t.id}/retire`); q.reload(); }}>Retire</Button>}
              </div>
            </div>
          </section>))}</div>
      )}</Load>
    </div>
  );
}
