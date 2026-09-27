import { useState } from "react";
import { source, type RunSummary } from "../api";
import { t } from "../i18n";

const EXAMPLES = [t("ex1"), t("ex2"), t("ex3")];

export function NewRun({ runs, onCreated }: { runs: RunSummary[]; onCreated: (id: string) => void }) {
  const [question, setQuestion] = useState("");
  const [approvePlan, setApprovePlan] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (source.demo) {
    return (
      <div className="page narrow">
        <h1>{t("demoTitle")}</h1>
        <p className="lead">{t("demoLead")}</p>
        {t("demoContentNote") && <p className="notice">{t("demoContentNote")}</p>}
        <div className="cards">
          {runs.map((r) => (
            <button key={r.id} className="card link" onClick={() => onCreated(r.id)}>
              <div className="card-title">{r.question}</div>
              <div className="muted small">{t("replay")}</div>
            </button>
          ))}
        </div>
        <p className="muted small">{t("demoFoot")}</p>
      </div>
    );
  }

  const submit = async (q: string) => {
    if (q.trim().length < 3) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await source.submit!(q.trim(), approvePlan));
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page narrow">
      <h1>{t("askTitle")}</h1>
      <p className="lead">{t("askLead")}</p>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit(question);
        }}
      >
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={t("askPlaceholder")}
          rows={3}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(question);
          }}
        />
        <div className="composer-row">
          <label className="check">
            <input type="checkbox" checked={approvePlan} onChange={(e) => setApprovePlan(e.target.checked)} />
            {t("reviewPlan")}
          </label>
          <button className="primary" disabled={busy || question.trim().length < 3}>
            {busy ? t("starting") : t("start")}
          </button>
        </div>
        {error && <div className="error">{error}</div>}
      </form>
      <div className="examples">
        <span className="muted small">{t("tryLabel")}</span>
        {EXAMPLES.map((q) => (
          <button key={q} className="chip" onClick={() => setQuestion(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}
