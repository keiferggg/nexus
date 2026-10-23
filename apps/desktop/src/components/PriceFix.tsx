import { useState } from "react";
import { post } from "../api";
import { Button } from "./ui";

/** Inline price entry for one model (USD per million tokens, from the provider's pricing page). */
export function PriceFix({ providerId, model, onSaved }: { providerId: string; model: string; onSaved: () => void }) {
  const [pin, setPin] = useState("");
  const [pout, setPout] = useState("");
  const [pcache, setPcache] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="col" style={{ gap: 4 }} data-testid="price-fix">
      <div className="row wrap">
        <span className="mono small">{model}</span>
        <input aria-label={`input price for ${model}`} type="number" min="0" step="0.01" placeholder="input $ / 1M tokens"
               value={pin} onChange={(e) => setPin(e.target.value)} style={{ width: 150 }} />
        <input aria-label={`cache-hit rate for ${model}`} type="number" min="0" step="0.001" placeholder="cache-hit input $ / 1M (optional)"
               value={pcache} onChange={(e) => setPcache(e.target.value)} style={{ width: 200 }} />
        <input aria-label={`output price for ${model}`} type="number" min="0" step="0.01" placeholder="output $ / 1M tokens"
               value={pout} onChange={(e) => setPout(e.target.value)} style={{ width: 150 }} />
        <Button size="sm" variant="primary" disabled={pin === "" || pout === ""} onClick={async () => {
          try {
            await post(`/api/providers/${providerId}/prices`, { model, input_per_mtok_usd: Number(pin), output_per_mtok_usd: Number(pout),
              cached_input_per_mtok_usd: pcache === "" ? null : Number(pcache),
              source: `owner-configured ${new Date().toISOString().slice(0, 10)}` });
            onSaved();
          } catch (e) { setErr((e as Error).message); }
        }}>Save price</Button>
      </div>
      <div className="xs faint">From the provider's pricing page. Input = the normal (cache-miss) rate. If the provider bills cached input cheaper (DeepSeek does), enter that rate too; NEXUS uses the cache-hit counts the provider reports on every call.</div>
      {err && <div className="small" style={{ color: "var(--bad)" }}>{err}</div>}
    </div>
  );
}
