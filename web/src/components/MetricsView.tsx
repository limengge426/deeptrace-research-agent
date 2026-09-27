import type { RunState } from "../api";
import { label, t } from "../i18n";

function Bars({ title, rows, unit }: { title: string; rows: Array<[string, number]>; unit: string }) {
  const max = Math.max(1, ...rows.map(([, v]) => v));
  return (
    <section className="panel">
      <div className="panel-title">{title}</div>
      {rows.length === 0 && <div className="empty">{t("noData")}</div>}
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
        <Stat label={t("statTokens")} value={(usage.tokens ?? 0).toLocaleString()} />
        <Stat label={t("statCalls")} value={String(usage.llm_calls ?? 0)} />
        <Stat label={t("statTools")} value={String(usage.tool_calls ?? 0)} />
        <Stat label={t("statWall")} value={`${Math.round(usage.seconds ?? 0)} s`} />
        <Stat label={t("statEvidence")} value={String(state.evidence.length)} />
        <Stat label={t("statRepairs")} value={`${state.repairs} / ${state.replans}`} />
        {hit !== null && hit !== undefined && <Stat label={t("statCache")} value={`${Math.round(hit * 100)}%`} />}
      </div>
      <div className="metrics-grid">
        <Bars title={t("barTokens")} rows={sorted(m.llm, "tokens")} unit={t("unitTok")} />
        <Bars title={t("barStages")} rows={sorted(m.stages, "seconds").map(([k, v]) => [label("stage", k), v])} unit="s" />
        <Bars title={t("barEvidence")} rows={sorted(m.context, "evidence_chars")} unit={t("unitChars")} />
        <Bars title={t("barTools")} rows={Object.entries(m.tools ?? {}).flatMap(([k, v]) =>
          [[`${k}`, v.calls ?? 0], [`${k} ${t("cached")}`, v.cache_hits ?? 0]] as Array<[string, number]>)} unit="" />
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
