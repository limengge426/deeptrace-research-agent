// Everything the live view shows is derived from the event stream alone, so a live run and a
// recorded replay go through exactly the same code path.
import type { EventRecord } from "./api";

export type TaskState = "pending" | "running" | "done" | "failed";

export interface DagTask {
  id: string;
  question: string;
  deps: string[];
  round: number;
  state: TaskState;
  evidence: number;
}

export const STAGES = ["plan", "execute", "report", "verify", "deliver", "done"];
export const FOLLOWUP_STAGES = ["route", "execute", "report", "verify", "done"];

/** A follow-up run starts by routing the question; a full research run starts by planning. */
export function isFollowup(events: EventRecord[]): boolean {
  return events.some((e) => e.kind === "run_created" && e.payload.followup);
}

export function deriveTasks(events: EventRecord[]): DagTask[] {
  const tasks = new Map<string, DagTask>();
  let round = 0;
  const add = (items: any[], r: number, replace: boolean) => {
    if (replace) tasks.clear();
    for (const t of items ?? []) {
      if (typeof t === "string") continue; // older recordings listed replan ids only
      tasks.set(t.id, { id: t.id, question: t.q, deps: t.deps ?? [], round: r, state: "pending", evidence: 0 });
    }
  };
  for (const e of events) {
    const p = e.payload;
    if (e.kind === "plan") add(p.tasks, 0, true);
    else if (e.kind === "plan_approved") add(p.tasks, 0, true);
    else if (e.kind === "replan") add(p.tasks, (round = p.round ?? round + 1), false);
    else if (e.kind === "task_started" && tasks.has(p.task)) tasks.get(p.task)!.state = "running";
    else if (e.kind === "task_done" && tasks.has(p.task)) {
      const t = tasks.get(p.task)!;
      t.state = "done";
      t.evidence = (p.evidence ?? []).length;
    } else if (e.kind === "task_failed" && tasks.has(p.task)) tasks.get(p.task)!.state = "failed";
  }
  return [...tasks.values()];
}

export function deriveStage(events: EventRecord[]): { current: string; finished: string | null; visited: Set<string> } {
  let current = isFollowup(events) ? "route" : "plan";
  let finished: string | null = null;
  const visited = new Set<string>([current]);
  for (const e of events) {
    if (e.kind === "stage") {
      current = e.payload.to;
      visited.add(e.payload.frm);
      visited.add(e.payload.to);
    }
    if (e.kind === "run_finished") finished = e.payload.status;
  }
  return { current: finished ? "done" : current, finished, visited };
}

/** Topological layers: tasks in the same layer can run in parallel. */
export function layers(tasks: DagTask[]): DagTask[][] {
  const placed = new Map<string, number>();
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const depth = (t: DagTask, seen = new Set<string>()): number => {
    if (placed.has(t.id)) return placed.get(t.id)!;
    if (seen.has(t.id)) return 0;
    seen.add(t.id);
    const d = Math.max(-1, ...t.deps.filter((x) => byId.has(x)).map((x) => depth(byId.get(x)!, seen))) + 1;
    placed.set(t.id, d);
    return d;
  };
  tasks.forEach((t) => depth(t));
  const out: DagTask[][] = [];
  for (const t of tasks) (out[placed.get(t.id)!] ??= []).push(t);
  return out.filter(Boolean);
}
