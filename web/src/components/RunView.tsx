import { useEffect, useMemo, useRef, useState } from "react";
import { source, type EventRecord, type RunState } from "../api";
import { deriveStage, deriveTasks, STAGES } from "../derive";
import { DagView } from "./DagView";
import { MetricsView } from "./MetricsView";
import { PlanEditor } from "./PlanEditor";
import { ReportView } from "./ReportView";
import { StatusPill } from "./StatusPill";
import { Timeline } from "./Timeline";

type Tab = "live" | "report" | "metrics";
const ACTIVE = ["plan", "execute", "report", "verify", "deliver"];

export function RunView({ id }: { id: string }) {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [state, setState] = useState<RunState | null>(null);
  const [tab, setTab] = useState<Tab>("live");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const userPickedTab = useRef(false);

  useEffect(() => {
    setEvents([]);
    return source.subscribe(id, (batch) => setEvents((prev) => [...prev, ...batch]), () => {});
  }, [id]);

  // Live: refresh the state while events arrive; replay: the recording's final state is loaded once.
  const refresh = () =>
    source
      .getState(id)
      .then((s) => (setState(s), setError(null)))
      .catch((e) => setError(e.message ?? String(e)));
  useEffect(() => {
    refresh();
  }, [id]);
  useEffect(() => {
    if (source.demo || events.length === 0) return;
    const t = setTimeout(refresh, 400);
    return () => clearTimeout(t);
  }, [events.length]);

  const tasks = useMemo(() => deriveTasks(events), [events]);
  const stage = useMemo(() => deriveStage(events), [events]);
  const replayDone = !source.demo || stage.finished !== null;
  const status = source.demo ? (stage.finished ?? (events.length ? stage.current : "plan")) : state?.status ?? "…";
  const showReport = Boolean(state?.report) && replayDone && (source.demo || state?.stage === "done");

  useEffect(() => {
    if (showReport && !userPickedTab.current) setTab("report");
  }, [showReport]);

  const pick = (t: Tab) => {
    userPickedTab.current = true;
    setTab(t);
  };

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  const liveStatus = state?.status ?? "";
  return (
    <div className="page">
      <header className="run-head">
        <div>
          <div className="muted small">Research question</div>
          <h1 className="question">{state?.question ?? "…"}</h1>
        </div>
        <div className="run-actions">
          <StatusPill status={status} />
          {!source.demo && state && (
            <>
              {ACTIVE.includes(liveStatus) && (
                <button disabled={busy} onClick={() => act(() => source.control!(id, "pause"))}>Pause</button>
              )}
              {["paused", "halted"].includes(liveStatus) && (
                <button className="primary" disabled={busy} onClick={() => act(() => source.control!(id, "resume"))}>
                  Resume
                </button>
              )}
              {!["done", "failed", "cancelled"].includes(liveStatus) && (
                <button className="danger" disabled={busy} onClick={() => act(() => source.control!(id, "cancel"))}>
                  Cancel
                </button>
              )}
            </>
          )}
        </div>
      </header>

      <StageBar current={stage.current} finished={stage.finished} visited={stage.visited} />
      {error && <div className="error">{error}</div>}
      {state?.error && state.status !== "done" && <div className="notice">{state.error}</div>}

      {!source.demo && state?.status === "awaiting_approval" && (
        <PlanEditor
          tasks={state.tasks}
          busy={busy}
          onApprove={(edited) => act(() => source.approve!(id, edited))}
        />
      )}

      <nav className="tabs">
        <button className={tab === "live" ? "active" : ""} onClick={() => pick("live")}>Live</button>
        <button className={tab === "report" ? "active" : ""} onClick={() => pick("report")} disabled={!showReport}>
          Report {showReport && state?.report ? "" : "·"}
        </button>
        <button className={tab === "metrics" ? "active" : ""} onClick={() => pick("metrics")} disabled={!state}>
          Metrics
        </button>
      </nav>

      {tab === "live" && (
        <div className="live-grid">
          <section className="panel">
            <div className="panel-title">
              Task graph <span className="muted small">independent tasks run in parallel</span>
            </div>
            <DagView tasks={tasks} />
          </section>
          <section className="panel">
            <div className="panel-title">
              Event log <span className="muted small">{events.length} events</span>
            </div>
            <Timeline events={events} />
          </section>
        </div>
      )}
      {tab === "report" && state && showReport && <ReportView state={state} />}
      {tab === "metrics" && state && <MetricsView state={state} />}
    </div>
  );
}

function StageBar({ current, finished, visited }: { current: string; finished: string | null; visited: Set<string> }) {
  const at = STAGES.indexOf(current as (typeof STAGES)[number]);
  return (
    <ol className="stages">
      {STAGES.map((s, i) => {
        const passed = finished && finished !== "done" ? i <= at : i < at;
        // A stage the run moved past without entering (e.g. deliver without a webhook) is shown as skipped.
        const state = passed ? (visited.has(s) ? "done" : "skipped") : i === at ? "now" : "";
        return (
          <li key={s} className={state}>
            <span className="dot" />
            {s}
          </li>
        );
      })}
    </ol>
  );
}
