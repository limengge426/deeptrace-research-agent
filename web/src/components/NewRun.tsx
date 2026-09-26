import { useState } from "react";
import { source, type RunSummary } from "../api";

const EXAMPLES = [
  "Are heat pumps worth it in cold climates?",
  "How did the transformer architecture enable large language models, and what are their limits?",
  "What caused the Great Depression, and how did it shape the Bretton Woods system?",
];

export function NewRun({ runs, onCreated }: { runs: RunSummary[]; onCreated: (id: string) => void }) {
  const [question, setQuestion] = useState("");
  const [approvePlan, setApprovePlan] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (source.demo) {
    return (
      <div className="page narrow">
        <h1>DeepTrace demo</h1>
        <p className="lead">
          These are <strong>recorded runs of the real agent</strong> (<code>gpt-4o-mini</code> over a fixed set of
          Wikipedia articles), replayed in your browser. Watch the planner build a task graph, parallel research
          fill it in, and the verifier check every cited sentence against its source.
        </p>
        <div className="cards">
          {runs.map((r) => (
            <button key={r.id} className="card link" onClick={() => onCreated(r.id)}>
              <div className="card-title">{r.question}</div>
              <div className="muted small">Replay this run →</div>
            </button>
          ))}
        </div>
        <p className="muted small">
          To run your own questions, start the API locally with your model key (see the README).
        </p>
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
      <h1>What should DeepTrace research?</h1>
      <p className="lead">
        It plans sub-questions, researches them in parallel, writes a cited report and checks every cited sentence
        against its source before finishing.
      </p>
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
          placeholder="Ask a research question…"
          rows={3}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(question);
          }}
        />
        <div className="composer-row">
          <label className="check">
            <input type="checkbox" checked={approvePlan} onChange={(e) => setApprovePlan(e.target.checked)} />
            Let me review the plan before research starts
          </label>
          <button className="primary" disabled={busy || question.trim().length < 3}>
            {busy ? "Starting…" : "Start research"}
          </button>
        </div>
        {error && <div className="error">{error}</div>}
      </form>
      <div className="examples">
        <span className="muted small">Try:</span>
        {EXAMPLES.map((q) => (
          <button key={q} className="chip" onClick={() => setQuestion(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}
