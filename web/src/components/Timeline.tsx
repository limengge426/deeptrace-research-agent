import { useEffect, useRef } from "react";
import type { EventRecord } from "../api";

type Line = { icon: string; text: string; tone?: "good" | "warn" | "bad" | "accent" } | null;

const pid = (owner: string | undefined) => (owner ? owner.split(":")[1] ?? owner : "?");
const pct = (x: number | undefined) => (x === undefined ? "?" : `${Math.round(x * 100)}%`);
const list = (xs: any[] | undefined) => (xs && xs.length ? xs.join(", ") : "none");

function describe(e: EventRecord, previousOwner: string | undefined): Line {
  const p = e.payload;
  switch (e.kind) {
    case "run_created":
      return { icon: "✦", text: p.approve_plan ? "Run created (plan needs approval)" : "Run created" };
    case "run_claimed":
      return previousOwner && previousOwner !== p.owner
        ? { icon: "⇄", text: `Taken over by worker pid ${pid(p.owner)} (lease token ${p.token}) at ${p.stage}`, tone: "accent" }
        : { icon: "▶", text: `Worker pid ${pid(p.owner)} claimed the run (lease token ${p.token})` };
    case "plan":
      return { icon: "◇", text: `Planned ${p.tasks?.length ?? 0} sub-questions` };
    case "plan_approved":
      return { icon: "✓", text: p.edited ? "Plan edited and approved" : "Plan approved", tone: "good" };
    case "run_held":
      return { icon: "⏸", text: `Waiting: ${String(p.status).replace(/_/g, " ")}`, tone: "warn" };
    case "stage":
      return null; // shown by the stage bar
    case "task_started":
      return { icon: "…", text: `${p.task} researching: ${p.question}` };
    case "task_done":
      return { icon: "●", text: `${p.task} done with ${(p.evidence ?? []).length} sources`, tone: "good" };
    case "task_failed":
      return { icon: "✕", text: `${p.task} failed: ${p.error}`, tone: "bad" };
    case "fetch_failed":
      return { icon: "!", text: `Could not fetch ${p.url}; kept the search snippet`, tone: "warn" };
    case "tool_retry_after_crash":
      return { icon: "↻", text: `Retrying interrupted ${p.tool} call (${p.read_only ? "read-only" : "same idempotency key"})`, tone: "accent" };
    case "tool_outcome_unknown":
      return { icon: "?", text: `Interrupted ${p.tool} call cannot be retried safely: needs a human`, tone: "bad" };
    case "outline":
      return { icon: "☰", text: `Outlined ${p.sections?.length ?? 0} sections` };
    case "faithfulness":
      return {
        icon: "⚖",
        text: `Checked ${p.claims} cited claims: ${p.supported} supported, ${p.partial} partial, ${p.unsupported + p.contradicted} unsupported (${pct(p.support_rate)} fully supported)`,
        tone: p.unsupported + p.contradicted > 0 ? "warn" : "good",
      };
    case "verify":
      return p.passed
        ? { icon: "✓", text: "Verification passed", tone: "good" }
        : { icon: "⚑", text: `Verification found: ${list(p.issues?.length ? [...new Set(p.issues)] : p.gaps)}`, tone: "warn" };
    case "report_repaired":
      return { icon: "✎", text: `Rewrote ${list(p.sections)}; kept ${p.kept?.length ?? 0} sections as they were`, tone: "accent" };
    case "replan":
      return { icon: "+", text: `Evidence gap: planned ${p.tasks?.length ?? 0} more sub-questions (round ${p.round})`, tone: "accent" };
    case "delivery":
      return p.status === "delivered"
        ? { icon: "➜", text: "Report delivered to the webhook", tone: "good" }
        : { icon: "✕", text: `Delivery failed: ${p.error}`, tone: "bad" };
    case "delivery_failed":
      return { icon: "!", text: `Delivery attempt ${p.attempt} failed`, tone: "warn" };
    case "control_requested":
      return { icon: "⏺", text: `${p.action} requested` };
    case "control_applied":
      return { icon: "⏹", text: `${p.action} applied`, tone: "warn" };
    case "run_halted":
      return { icon: "⏸", text: `Halted: ${p.reason}`, tone: "warn" };
    case "lease_lost":
    case "run_abandoned":
      return { icon: "⇄", text: `Worker pid ${pid(p.owner)} lost its lease and stopped`, tone: "accent" };
    case "run_interrupted":
      return { icon: "✕", text: `Worker interrupted at ${p.stage}`, tone: "bad" };
    case "worker_error":
      return { icon: "!", text: `Worker error: ${p.error}`, tone: "bad" };
    case "run_finished":
      return {
        icon: "■",
        text: `Finished: ${p.status}${p.usage ? ` · ${Math.round(p.usage.tokens).toLocaleString()} tokens · ${p.usage.seconds}s` : ""}`,
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

  if (events.length === 0) return <div className="empty">Waiting for events…</div>;
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
