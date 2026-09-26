import type { RunState } from "../api";

function Bars({ title, rows, unit }: { title: string; rows: Array<[string, number]>; unit: string }) {
  const max = Math.max(1, ...rows.map(([, v]) => v));
  return (
    <section className="panel">
      <div className="panel-title">{title}</div>
      {rows.length === 0 && <div className="empty">No data yet.</div>}
      {rows.map(([name, value]) => (
        <div key={name} className="bar-row">
          <span className="bar-name">{name}</span>
          <span className="bar-track"><span className="bar" style={{ width: `${(100 * value) / max}%` }} /></span>
          <span className="bar-value">{value.toLocaleString(undefined, { maximumFractionDigits: 1 })} {unit}</span>
        </div>
      ))}
    </section>
  );
}

export function MetricsView({ state }: { state: RunState }) {
  const m = state.metrics;
  const sorted = (o: Record<string, Record<string, number>> | undefined, key: string) =>
    Object.entries(o ?? {}).map(([k, v]) => [k, v[key] ?? 0] as [string, number]).sort((a, b) => b[1] - a[1]);
  const usage = m.usage ?? {};
  const hit = m.derived?.tool_cache_hit_rate;

  return (
    <div className="metrics">
      <div className="stat-row">
        <Stat label="LLM tokens" value={(usage.tokens ?? 0).toLocaleString()} />
        <Stat label="LLM calls" value={String(usage.llm_calls ?? 0)} />
        <Stat label="Tool calls" value={String(usage.tool_calls ?? 0)} />
        <Stat label="Wall time" value={`${Math.round(usage.seconds ?? 0)} s`} />
        <Stat label="Evidence" value={String(state.evidence.length)} />
        <Stat label="Repairs / replans" value={`${state.repairs} / ${state.replans}`} />
        {hit !== null && hit !== undefined && <Stat label="Tool cache hits" value={`${Math.round(hit * 100)}%`} />}
      </div>
      <div className="metrics-grid">
        <Bars title="Tokens by LLM purpose" rows={sorted(m.llm, "tokens")} unit="tok" />
        <Bars title="Time by stage" rows={sorted(m.stages, "seconds")} unit="s" />
        <Bars title="Evidence placed into prompts" rows={sorted(m.context, "evidence_chars")} unit="chars" />
        <Bars title="Tool calls" rows={Object.entries(m.tools ?? {}).flatMap(([k, v]) =>
          [[`${k}`, v.calls ?? 0], [`${k} (cached)`, v.cache_hits ?? 0]] as Array<[string, number]>)} unit="" />
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
