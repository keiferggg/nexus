import { ReactNode, useEffect, useRef } from "react";

export function Button(props: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "danger" | "ghost"; size?: "sm" }) {
  const { variant, size, className, ...rest } = props;
  return <button type="button" className={["btn", variant, size, className].filter(Boolean).join(" ")} {...rest} />;
}

const STATUS_TONE: Record<string, string> = {
  accepted: "ok", completed: "ok", ok: "ok", pass: "ok", supported: "ok", confirmed: "ok", succeeded: "ok", promoted: "ok",
  active: "ok", resolved: "ok", reconciled: "ok", acked: "ok", ready_service: "ok",
  running: "info", leased: "info", verifying: "info", submitted: "info", planning: "info", ready: "neutral", draft: "neutral",
  evaluating: "info", canary: "info", proposed: "neutral", open: "neutral", received: "neutral", pending: "neutral",
  delivered: "neutral", reserved: "warn",
  blocked: "warn", paused: "warn", inconclusive: "warn", disputed: "warn", stale: "warn", uncertain: "warn", canceling: "warn",
  unvalidated: "warn", unresolved: "warn", changed: "warn", surprised: "warn", candidate: "neutral", escalated: "warn",
  failed: "bad", fail: "bad", rejected: "bad", canceled: "neutral", superseded: "neutral", withdrawn: "bad", denied: "bad",
  auth_failed: "bad", unreachable: "bad", error: "bad", fenced: "bad", abandoned: "neutral", rolled_back: "warn", expired: "neutral",
};

export function Status({ value, label }: { value: string | null | undefined; label?: string }) {
  const v = value ?? "unknown";
  return (
    <span className={`badge ${STATUS_TONE[v] ?? "neutral"}`} data-status={v}>
      <span className="dot" aria-hidden />
      {label ?? v.replace(/_/g, " ")}
    </span>
  );
}

export function Badge({ tone = "neutral", children, title }: { tone?: string; children: ReactNode; title?: string }) {
  return <span className={`badge ${tone}`} title={title}>{children}</span>;
}

export function SimBadge() {
  return <Badge tone="sim" title="Runs the deterministic fake provider. Outputs are scripted, not model-generated.">DEMO · offline fake</Badge>;
}

export function Card({ title, actions, children, flush, id }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean; id?: string }) {
  return (
    <section className="card" aria-labelledby={id}>
      {title !== undefined && (
        <div className="card-head">
          <h3 id={id}>{title}</h3>
          {actions}
        </div>
      )}
      <div className={`card-body${flush ? " flush" : ""}`}>{children}</div>
    </section>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state" role="status">
      <h3>{title}</h3>
      {children && <div>{children}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="state error" role="alert">
      <h3>Could not load</h3>
      <div className="small">{error}</div>
      {onRetry && <div style={{ marginTop: 10 }}><Button onClick={onRetry}>Retry</Button></div>}
    </div>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="state" role="status" aria-live="polite">{what}…</div>;
}

/** Renders loading / error / empty states consistently; never substitutes placeholder data. */
export function Load<T>({ q, empty, children }: { q: { data: T | null; error: string | null; loading: boolean; reload: () => void };
  empty?: (d: T) => string | null; children: (d: T) => ReactNode }) {
  if (q.error) return <ErrorState error={q.error} onRetry={q.reload} />;
  if (q.loading || q.data === null) return <Loading />;
  const e = empty?.(q.data);
  if (e) return <Empty title={e} />;
  return <>{children(q.data)}</>;
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function Drawer({ title, onClose, children, actions }: { title: ReactNode; onClose: () => void; children: ReactNode; actions?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.focus();
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer" role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : "details"} tabIndex={-1} ref={ref}>
        <div className="drawer-head">
          <h2 className="grow">{title}</h2>
          {actions}
          <Button variant="ghost" onClick={onClose} aria-label="Close">✕</Button>
        </div>
        <div className="drawer-body">{children}</div>
      </div>
    </>
  );
}

export function Dialog({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const first = ref.current?.querySelector<HTMLElement>("input,textarea,select,button");
    first?.focus();
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <>
      <div className="dialog-backdrop" onClick={onClose} />
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <div className="card-head"><h3>{title}</h3><Button variant="ghost" onClick={onClose} aria-label="Close">✕</Button></div>
        <div className="card-body col">{children}</div>
        <div className="card-head" style={{ borderTop: "1px solid var(--border)", borderBottom: 0, justifyContent: "flex-end" }}>{footer}</div>
      </div>
    </>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string; kbd?: string }[]; value: T; onChange: (t: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" className="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)} id={`tab-${t.id}`}>
          {t.label}{t.kbd && <span className="kbd" aria-hidden>{t.kbd}</span>}
        </button>
      ))}
    </div>
  );
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return <code className="mono" title={title}>{children}</code>;
}

export function KV({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}><dt>{k}</dt><dd>{v}</dd></div>
      ))}
    </dl>
  );
}

/** Minimal, safe markdown rendering (no HTML injection): headings, lists, paragraphs, citations. */
export function Doc({ text }: { text: string }) {
  const lines = text.split(/\r?\n/);
  const out: ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      out.push(<ul key={`ul${out.length}`}>{list.map((l, i) => <li key={i}>{inline(l)}</li>)}</ul>);
      list = [];
    }
  };
  lines.forEach((l, i) => {
    const h = /^(#{1,3})\s+(.*)$/.exec(l);
    if (h) {
      flush();
      const Tag = (`h${h[1].length}`) as "h1" | "h2" | "h3";
      out.push(<Tag key={i}>{inline(h[2])}</Tag>);
    } else if (/^\s*[-*]\s+/.test(l)) {
      list.push(l.replace(/^\s*[-*]\s+/, ""));
    } else if (l.trim() === "") {
      flush();
    } else {
      flush();
      out.push(<p key={i}>{inline(l)}</p>);
    }
  });
  flush();
  return <div className="doc">{out}</div>;
}

function inline(s: string): ReactNode[] {
  const parts = s.split(/(\[E:ev_[0-9a-f]+\]|\*\*[^*]+\*\*|`[^`]+`)/g);
  return parts.map((p, i) => {
    if (/^\[E:ev_/.test(p)) return <span key={i} className="cite" title={`evidence ${p.slice(3, -1)}`}>{p.slice(3, -1).slice(0, 9)}</span>;
    if (/^\*\*/.test(p)) return <strong key={i}>{p.slice(2, -2)}</strong>;
    if (/^`/.test(p)) return <code key={i}>{p.slice(1, -1)}</code>;
    return <span key={i}>{p}</span>;
  });
}

export function lineDiff(a: string, b: string): { kind: "add" | "del" | "ctx"; text: string }[] {
  // LCS-based line diff, bounded for very large inputs.
  const x = a.split("\n"), y = b.split("\n");
  if (x.length * y.length > 4_000_000) return [{ kind: "ctx", text: "(too large to diff in the UI)" }];
  const dp: number[][] = Array.from({ length: x.length + 1 }, () => new Array(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
    dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: { kind: "add" | "del" | "ctx"; text: string }[] = [];
  let i = 0, j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { out.push({ kind: "ctx", text: x[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ kind: "del", text: x[i++] });
    else out.push({ kind: "add", text: y[j++] });
  }
  while (i < x.length) out.push({ kind: "del", text: x[i++] });
  while (j < y.length) out.push({ kind: "add", text: y[j++] });
  return out;
}
