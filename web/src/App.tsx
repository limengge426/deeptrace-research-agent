import { useEffect, useState } from "react";
import { source, type RunSummary } from "./api";
import { NewRun } from "./components/NewRun";
import { RunView } from "./components/RunView";
import { StatusPill } from "./components/StatusPill";
import { lang, langHref, t } from "./i18n";

const REPO = "https://github.com/limengge426/deeptrace-research-agent";

function selectedFromHash(): string | null {
  const m = window.location.hash.match(/^#\/run\/([\w-]+)/);
  return m ? m[1] : null;
}

export function App() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(selectedFromHash());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onHash = () => setSelected(selectedFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    let alive = true;
    const load = () =>
      source
        .listRuns()
        .then((r) => alive && (setRuns(r), setError(null)))
        .catch((e) => alive && setError(String(e.message ?? e)));
    load();
    const timer = source.demo ? undefined : setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  // One sidebar entry per conversation: its first question, and the status of its latest turn.
  const threads = groupThreads(runs);

  const open = (id: string | null) => {
    window.location.hash = id ? `#/run/${id}` : "";
    setSelected(id);
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo" aria-hidden>◎</span>
          <div>
            <div className="brand-name">DeepTrace</div>
            <div className="brand-sub">{t("tagline")}</div>
          </div>
        </div>
        <button className="primary wide" onClick={() => open(null)}>
          {source.demo ? t("aboutDemo") : t("newResearch")}
        </button>
        <div className="runs-label">{source.demo ? t("recordedRuns") : t("runs")}</div>
        <nav className="runs">
          {error && <div className="muted small">{t("apiError", { error })}</div>}
          {threads.map((th) => (
            <button key={th.first.id} className={`run-item ${th.ids.includes(selected ?? "") ? "active" : ""}`}
                    onClick={() => open(th.latest.id)}>
              <span className="run-q">{th.first.question}</span>
              <span className="run-meta">
                <StatusPill status={th.latest.status} small />
                {th.ids.length > 1 && <span className="muted small">{t("turnsCount", { n: th.ids.length })}</span>}
              </span>
            </button>
          ))}
          {!error && runs.length === 0 && <div className="muted small">{t("noRuns")}</div>}
        </nav>
        <footer className="sidebar-foot">
          <a href={REPO} target="_blank" rel="noreferrer">GitHub</a>
          {!source.demo && <a href="/docs" target="_blank" rel="noreferrer">{t("apiDocs")}</a>}
          <a href={langHref(lang === "zh" ? "en" : "zh")} className="lang-switch">{t("switchLang")}</a>
        </footer>
      </aside>
      <main className="main">
        {selected ? <RunView key={selected} id={selected} onOpen={open} /> : <NewRun runs={threads.map((th) => th.first)} onCreated={open} />}
      </main>
    </div>
  );
}

function groupThreads(runs: RunSummary[]) {
  // `runs` is newest first; a thread is listed where its latest turn falls.
  const byThread = new Map<string, RunSummary[]>();
  for (const r of runs) {
    const key = r.thread_id ?? r.id;
    byThread.set(key, [...(byThread.get(key) ?? []), r]);
  }
  return [...byThread.values()].map((turns) => ({
    latest: turns[0],
    first: turns[turns.length - 1],
    ids: turns.map((r) => r.id),
  }));
}
