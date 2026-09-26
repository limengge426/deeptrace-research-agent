import { Fragment, useMemo, useState, type ReactNode } from "react";
import type { Evidence, Label, RunState, Sentence } from "../api";

const LABEL_TEXT: Record<Label, string> = {
  supported: "Supported by its source",
  partial: "Goes slightly beyond its source",
  unsupported: "Not supported by its source",
  contradicted: "Contradicted by its source",
};

/** Minimal inline Markdown: **bold** and *italic*. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).map((part, i) =>
    part.startsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong>
      : part.startsWith("*") && part.length > 2 ? <em key={i}>{part.slice(1, -1)}</em>
      : <Fragment key={i}>{part}</Fragment>,
  );
}

function SentenceView({ s, onCite }: { s: Sentence; onCite: (id: string) => void }) {
  const parts = s.text.split(/(\[E\d+\])/g);
  return (
    <span className={`sent ${s.label ?? "unjudged"}`} title={s.label ? `${LABEL_TEXT[s.label]}${s.reason ? `: ${s.reason}` : ""}` : undefined}>
      {parts.map((part, i) => {
        const m = part.match(/^\[(E\d+)\]$/);
        return m ? (
          <button key={i} className="cite" onClick={() => onCite(m[1])}>{m[1]}</button>
        ) : (
          <Fragment key={i}>{inline(part)}</Fragment>
        );
      })}{" "}
    </span>
  );
}

export function ReportView({ state }: { state: RunState }) {
  const [open, setOpen] = useState<string | null>(null);
  const report = state.report!;
  const evidence = useMemo(() => new Map(state.evidence.map((e) => [e.id, e])), [state.evidence]);

  const sentences = report.sections.flatMap((s) => s.paragraphs.flat());
  const counts = { supported: 0, partial: 0, unsupported: 0, contradicted: 0 } as Record<Label, number>;
  for (const s of sentences) if (s.label) counts[s.label]++;
  const judged = Object.values(counts).reduce((a, b) => a + b, 0);
  const first = state.faithfulness[0]?.support_rate;
  const last = state.faithfulness[state.faithfulness.length - 1]?.support_rate;
  const cited = [...new Set(sentences.flatMap((s) => s.citations))].filter((c) => evidence.has(c));

  return (
    <div className={`report-layout ${open ? "with-drawer" : ""}`}>
      <article className="report">
        <h2>{report.title}</h2>
        {judged > 0 && (
          <div className="verdict-bar">
            <div className="legend">
              {(Object.keys(counts) as Label[]).map((l) => (
                <span key={l} className={`legend-item ${l}`}>
                  <span className="swatch" /> {counts[l]} {l}
                </span>
              ))}
              <span className="legend-item unjudged">
                <span className="swatch" /> uncited / not checked
              </span>
            </div>
            <div className="muted small">
              {state.verdict_source
                ? <>Sentence colours: every cited sentence as labelled by the {state.verdict_source} against the full text of its sources.</>
                : <>Every cited sentence was checked against the full text of its sources.</>}
              {first !== undefined && last !== undefined && state.repairs > 0 && (
                <> The in-loop judge rated {Math.round(first * 100)}% of claims fully supported on the first draft and{" "}
                {Math.round(last * 100)}% after {state.repairs} targeted repair{state.repairs > 1 ? "s" : ""}.</>
              )}
            </div>
          </div>
        )}
        {report.sections.map((section) => (
          <section key={section.heading}>
            <h3>{section.heading}{section.synthesis && <span className="tag">synthesis</span>}</h3>
            {section.paragraphs.map((para, i) => (
              <p key={i}>{para.map((s, j) => <SentenceView key={j} s={s} onCite={setOpen} />)}</p>
            ))}
          </section>
        ))}
        {state.limitations.length > 0 && (
          <section className="limitations">
            <h3>Limitations</h3>
            <ul>{state.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
          </section>
        )}
        <section className="sources">
          <h3>Sources</h3>
          <ol>
            {cited.map((id) => {
              const e = evidence.get(id)!;
              return (
                <li key={id}>
                  <button className="cite" onClick={() => setOpen(id)}>{id}</button> {e.title}
                  <span className="muted small"> · {e.url}</span>
                </li>
              );
            })}
          </ol>
        </section>
      </article>
      {open && evidence.has(open) && (
        <EvidenceDrawer e={evidence.get(open)!} sentences={sentences.filter((s) => s.citations.includes(open))}
                        onClose={() => setOpen(null)} />
      )}
    </div>
  );
}

function EvidenceDrawer({ e, sentences, onClose }: { e: Evidence; sentences: Sentence[]; onClose: () => void }) {
  const link = e.url.startsWith("http");
  return (
    <aside className="drawer">
      <div className="drawer-head">
        <span className="cite static">{e.id}</span>
        <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <h4>{e.title}</h4>
      <div className="muted small">
        {link ? <a href={e.url} target="_blank" rel="noreferrer">{e.url}</a> : e.url} · found by task {e.task}
      </div>
      <div className="evidence-text">{e.content}</div>
      <div className="drawer-sub">Cited by {sentences.length} sentence{sentences.length === 1 ? "" : "s"}</div>
      {sentences.map((s, i) => (
        <div key={i} className={`cited-by ${s.label ?? "unjudged"}`}>
          <div>{s.text.replace(/\[E\d+\]/g, "").trim()}</div>
          {s.label && <div className="muted small">{LABEL_TEXT[s.label]}{s.reason ? `: ${s.reason}` : ""}</div>}
        </div>
      ))}
    </aside>
  );
}
