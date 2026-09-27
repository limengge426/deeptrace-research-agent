import { useEffect, useRef } from "react";
import type { EventRecord } from "../api";
import { label, t } from "../i18n";

type Line = { icon: string; text: string; tone?: "good" | "warn" | "bad" | "accent" } | null;

const pid = (owner: string | undefined) => (owner ? owner.split(":")[1] ?? owner : "?");
const pct = (x: number | undefined) => (x === undefined ? "?" : `${Math.round(x * 100)}%`);
const list = (xs: any[] | undefined) => (xs && xs.length ? xs.join(t("listSep")) : t("none"));

function describe(e: EventRecord, previousOwner: string | undefined): Line {
  const p = e.payload;
  switch (e.kind) {
    case "run_created":
      return { icon: "✦", text: p.approve_plan ? t("ev.createdApproval") : t("ev.created") };
    case "run_claimed":
      return previousOwner && previousOwner !== p.owner
        ? { icon: "⇄", text: t("ev.takeover", { pid: pid(p.owner), token: p.token, stage: label("stage", p.stage) }), tone: "accent" }
        : { icon: "▶", text: t("ev.claimed", { pid: pid(p.owner), token: p.token }) };
    case "plan":
      return { icon: "◇", text: t("ev.plan", { n: p.tasks?.length ?? 0 }) };
    case "plan_approved":
      return { icon: "✓", text: p.edited ? t("ev.planEdited") : t("ev.planApproved"), tone: "good" };
    case "run_held":
      return { icon: "⏸", text: t("ev.held", { status: label("status", String(p.status)) }), tone: "warn" };
    case "stage":
      return null; // shown by the stage bar
    case "task_started":
      return { icon: "…", text: t("ev.taskStarted", { task: p.task, question: p.question }) };
    case "task_done":
      return { icon: "●", text: t("ev.taskDone", { task: p.task, n: (p.evidence ?? []).length }), tone: "good" };
    case "task_failed":
      return { icon: "✕", text: t("ev.taskFailed", { task: p.task, error: p.error }), tone: "bad" };
    case "fetch_failed":
      return { icon: "!", text: t("ev.fetchFailed", { url: p.url }), tone: "warn" };
    case "tool_retry_after_crash":
      return { icon: "↻", text: t("ev.retry", { tool: p.tool, how: p.read_only ? t("ev.retryReadOnly") : t("ev.retryKey") }), tone: "accent" };
    case "tool_outcome_unknown":
      return { icon: "?", text: t("ev.unknown", { tool: p.tool }), tone: "bad" };
    case "outline":
      return { icon: "☰", text: t("ev.outline", { n: p.sections?.length ?? 0 }) };
    case "faithfulness":
      return {
        icon: "⚖",
        text: t("ev.faithfulness", {
          claims: p.claims,
          supported: p.supported,
          partial: p.partial,
          failing: p.unsupported + p.contradicted,
          rate: pct(p.support_rate),
        }),
        tone: p.unsupported + p.contradicted > 0 ? "warn" : "good",
      };
    case "verify":
      return p.passed
        ? { icon: "✓", text: t("ev.verifyPassed"), tone: "good" }
        : { icon: "⚑", text: t("ev.verifyFound", { what: list(p.issues?.length ? [...new Set<string>(p.issues)].map((i) => label("issue", i)) : p.gaps) }), tone: "warn" };
    case "report_repaired":
      return { icon: "✎", text: t("ev.repaired", { sections: list(p.sections), n: p.kept?.length ?? 0 }), tone: "accent" };
    case "replan":
      return { icon: "+", text: t("ev.replan", { n: p.tasks?.length ?? 0, round: p.round }), tone: "accent" };
    case "delivery":
      return p.status === "delivered"
        ? { icon: "➜", text: t("ev.delivered"), tone: "good" }
        : { icon: "✕", text: t("ev.deliveryFailed", { error: p.error }), tone: "bad" };
    case "delivery_failed":
      return { icon: "!", text: t("ev.deliveryAttempt", { n: p.attempt }), tone: "warn" };
    case "control_requested":
      return { icon: "⏺", text: t("ev.controlRequested", { action: label("action", p.action) }) };
    case "control_applied":
      return { icon: "⏹", text: t("ev.controlApplied", { action: label("action", p.action) }), tone: "warn" };
    case "run_halted":
      return { icon: "⏸", text: t("ev.halted", { reason: p.reason }), tone: "warn" };
    case "lease_lost":
    case "run_abandoned":
      return { icon: "⇄", text: t("ev.leaseLost", { pid: pid(p.owner) }), tone: "accent" };
    case "run_interrupted":
      return { icon: "✕", text: t("ev.interrupted", { stage: label("stage", p.stage) }), tone: "bad" };
    case "worker_error":
      return { icon: "!", text: t("ev.workerError", { error: p.error }), tone: "bad" };
    case "run_finished":
      return {
        icon: "■",
        text: p.usage
          ? t("ev.finishedUsage", {
              status: label("status", p.status),
              tokens: Math.round(p.usage.tokens).toLocaleString(),
              seconds: p.usage.seconds,
            })
          : t("ev.finished", { status: label("status", p.status) }),
        tone: p.status === "done" ? "good" : "bad",
      };
    default:
      return { icon: "·", text: e.kind.replace(/_/g, " ") };
  }
}

export function Timeline({ events }: { events: EventRecord[] }) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [events.length]);

  if (events.length === 0) return <div className="empty">{t("waitingEvents")}</div>;
  const t0 = events[0].ts;
  let owner: string | undefined;
  const rows = events.map((e) => {
    const line = describe(e, owner);
    if (e.kind === "run_claimed") owner = e.payload.owner;
    return line && { e, line };
  });

  return (
    <div
      className="timeline"
      ref={box}
      onScroll={(ev) => {
        const el = ev.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
    >
      {rows.map(
        (r) =>
          r && (
            <div key={r.e.id} className={`tl ${r.line.tone ?? ""}`}>
              <span className="tl-time">{(r.e.ts - t0).toFixed(1)}s</span>
              <span className="tl-icon">{r.line.icon}</span>
              <span className="tl-text">{r.line.text}</span>
            </div>
          ),
      )}
    </div>
  );
}
