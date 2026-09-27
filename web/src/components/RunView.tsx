import { useEffect, useMemo, useRef, useState } from "react";
import { source, type EventRecord, type RunState, type RunSummary } from "../api";
import { deriveStage, deriveTasks, FOLLOWUP_STAGES, isFollowup, STAGES } from "../derive";
import { DagView } from "./DagView";
import { MetricsView } from "./MetricsView";
import { PlanEditor } from "./PlanEditor";
import { ReportView } from "./ReportView";
import { StatusPill } from "./StatusPill";
import { Timeline } from "./Timeline";
import { label, t } from "../i18n";

type Tab = "live" | "report" | "metrics";
const ACTIVE = ["plan", "route", "execute", "report", "verify", "deliver"];

export function RunView({ id, onOpen }: { id: string; onOpen: (id: string) => void }) {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [state, setState] = useState<RunState | null>(null);
  const [tab, setTab] = useState<Tab>("live");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const userPickedTab = useRef(false);
  const [thread, setThread] = useState<RunSummary[]>([]);

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

  useEffect(() => {
    source.getThread(state?.thread_id ?? id).then(setThread).catch(() => setThread([]));
  }, [id, state?.thread_id, state?.status]);

  const tasks = useMemo(() => deriveTasks(events), [events]);
  const stage = useMemo(() => deriveStage(events), [events]);
  const followup = (state?.turn ?? 1) > 1 || isFollowup(events);
  const answerMode = followup && state?.mode !== "report";
  const stages = followup
    ? stage.visited.has("plan") ? ["route", "plan", ...FOLLOWUP_STAGES.slice(1)] : FOLLOWUP_STAGES
    : STAGES;
  const latestTurn = thread.length ? thread[thread.length - 1].id : id;
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
          <div className="muted small">{followup ? t("followupQuestion") : t("researchQuestion")}</div>
          <h1 className="question">{state?.question ?? "…"}</h1>
          {state?.route && (
            <div className="muted small route-note">
              {state.route.question !== state.question && <>{t("understoodAs", { q: state.route.question })} · </>}
              {label("route", state.route.decision)}
            </div>
          )}
        </div>
        <div className="run-actions">
          <StatusPill status={status} />
          {!source.demo && state && (
            <>
              {ACTIVE.includes(liveStatus) && (
                <button disabled={busy} onClick={() => act(() => source.control!(id, "pause"))}>{t("pause")}</button>
              )}
              {["paused", "halted"].includes(liveStatus) && (
                <button className="primary" disabled={busy} onClick={() => act(() => source.control!(id, "resume"))}>
                  {t("resume")}
                </button>
              )}
              {!["done", "failed", "cancelled"].includes(liveStatus) && (
                <button className="danger" disabled={busy} onClick={() => act(() => source.control!(id, "cancel"))}>
                  {t("cancel")}
                </button>
              )}
            </>
          )}
        </div>
      </header>

      {thread.length > 1 && <ThreadNav thread={thread} current={id} onOpen={onOpen} />}
      <StageBar stages={stages} current={stage.current} finished={stage.finished} visited={stage.visited}
                answer={answerMode} />
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
        <button className={tab === "live" ? "active" : ""} onClick={() => pick("live")}>{t("tabLive")}</button>
        <button className={tab === "report" ? "active" : ""} onClick={() => pick("report")} disabled={!showReport}>
          {answerMode ? t("tabAnswer") : t("tabReport")} {showReport && state?.report ? "" : "·"}
        </button>
        <button className={tab === "metrics" ? "active" : ""} onClick={() => pick("metrics")} disabled={!state}>
          {t("tabMetrics")}
        </button>
      </nav>

      {tab === "live" && (
        <div className="live-grid">
          <section className="panel">
            <div className="panel-title">
              {t("taskGraph")} <span className="muted small">{t("taskGraphHint")}</span>
            </div>
            {followup && tasks.length === 0 && state?.route?.decision === "answer" ? (
              <div className="empty">{t("noNewResearch", { n: state.evidence.length })}</div>
            ) : (
              <DagView tasks={tasks} />
            )}
          </section>
          <section className="panel">
            <div className="panel-title">
              {t("eventLog")} <span className="muted small">{t("eventCount", { n: events.length })}</span>
            </div>
            <Timeline events={events} />
          </section>
        </div>
      )}
      {tab === "report" && state && showReport && <ReportView state={state} />}
      {!source.demo && state?.status === "done" && latestTurn === id && (
        <FollowupBox id={id} onCreated={onOpen} />
      )}
      {tab === "metrics" && state && <MetricsView state={state} />}
    </div>
  );
}

function ThreadNav({ thread, current, onOpen }: { thread: RunSummary[]; current: string; onOpen: (id: string) => void }) {
  return (
    <nav className="thread" aria-label={t("conversation")}>
      {thread.map((r, i) => (
        <button key={r.id} className={`turn ${r.id === current ? "active" : ""}`} onClick={() => onOpen(r.id)} title={r.question}>
          <span className="turn-n">{t("turn", { n: i + 1 })}</span>
          <span className="turn-q">{r.question}</span>
          <StatusPill status={r.status} small />
        </button>
      ))}
    </nav>
  );
}

function FollowupBox({ id, onCreated }: { id: string; onCreated: (id: string) => void }) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = async () => {
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await source.followup!(id, question.trim()));
      setQuestion("");
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="composer followup"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <textarea
        value={question}
        rows={2}
        placeholder={t("followupPlaceholder")}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
        }}
      />
      <div className="composer-row">
        <span className="muted small">{t("followupHint")}</span>
        <button className="primary" disabled={busy || !question.trim()}>{busy ? t("sending") : t("askFollowup")}</button>
      </div>
      {error && <div className="error">{error}</div>}
    </form>
  );
}

function StageBar({ stages, current, finished, visited, answer }: {
  stages: string[];
  current: string;
  finished: string | null;
  visited: Set<string>;
  answer: boolean; // a follow-up writes a short answer rather than a report
}) {
  const at = stages.indexOf(current);
  return (
    <ol className="stages">
      {stages.map((s, i) => {
        const passed = finished && finished !== "done" ? i <= at : i < at;
        // A stage the run moved past without entering (e.g. deliver without a webhook) is shown as skipped.
        const state = passed ? (visited.has(s) ? "done" : "skipped") : i === at ? "now" : "";
        return (
          <li key={s} className={state}>
            <span className="dot" />
            {answer && s === "report" ? t("tabAnswer") : label("stage", s)}
          </li>
        );
      })}
    </ol>
  );
}
