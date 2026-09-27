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
          {runs.map((r) => (
            <button key={r.id} className={`run-item ${r.id === selected ? "active" : ""}`} onClick={() => open(r.id)}>
              <span className="run-q">{r.question}</span>
              <StatusPill status={r.status} small />
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
        {selected ? <RunView key={selected} id={selected} /> : <NewRun runs={runs} onCreated={open} />}
      </main>
    </div>
  );
}
