import { useState } from "react";
import type { Task } from "../api";
import { t } from "../i18n";

type Row = { id: string; question: string; deps: string };

export function PlanEditor({ tasks, busy, onApprove }: {
  tasks: Task[];
  busy: boolean;
  onApprove: (edited: Array<Pick<Task, "id" | "question" | "depends_on">> | null) => void;
}) {
  const initial = tasks.map((t) => ({ id: t.id, question: t.question, deps: t.depends_on.join(", ") }));
  const [rows, setRows] = useState<Row[]>(initial);
  const edited = JSON.stringify(rows) !== JSON.stringify(initial);

  const update = (i: number, patch: Partial<Row>) => setRows((r) => r.map((row, j) => (j === i ? { ...row, ...patch } : row)));
  const nextId = () => {
    let n = rows.length + 1;
    while (rows.some((r) => r.id === `t${n}`)) n++;
    return `t${n}`;
  };

  return (
    <section className="panel plan-editor">
      <div className="panel-title">
        {t("reviewTitle")} <span className="muted small">{t("reviewHint")}</span>
      </div>
      <table>
        <thead>
          <tr><th>id</th><th>{t("colSubQuestion")}</th><th>{t("colDependsOn")}</th><th /></tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="mono">{r.id}</td>
              <td><input value={r.question} onChange={(e) => update(i, { question: e.target.value })} /></td>
              <td><input className="mono" value={r.deps} placeholder="—" onChange={(e) => update(i, { deps: e.target.value })} /></td>
              <td><button className="ghost" onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))} aria-label={t("remove")}>✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button onClick={() => setRows((rs) => [...rs, { id: nextId(), question: "", deps: "" }])}>{t("addSubQuestion")}</button>
        <span className="grow" />
        {edited && <button onClick={() => setRows(initial)}>{t("reset")}</button>}
        <button
          className="primary"
          disabled={busy || rows.length === 0 || rows.some((r) => r.question.trim().length < 3)}
          onClick={() =>
            onApprove(edited ? rows.map((r) => ({
              id: r.id,
              question: r.question.trim(),
              depends_on: r.deps.split(",").map((d) => d.trim()).filter(Boolean),
            })) : null)
          }
        >
          {edited ? t("approveEdited") : t("approve")}
        </button>
      </div>
    </section>
  );
}
