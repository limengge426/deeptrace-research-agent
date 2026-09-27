// Data layer. The console talks to a `Source`: either the live API or a recorded replay,
// so the same UI runs against a real deployment and as a zero-cost static demo.

export type Label = "supported" | "partial" | "unsupported" | "contradicted";

export interface EventRecord {
  id: number;
  ts: number;
  kind: string;
  payload: Record<string, any>;
}

export interface Sentence {
  text: string;
  citations: string[];
  label: Label | null;
  reason: string | null;
}

export interface ReportSection {
  heading: string;
  synthesis: boolean;
  paragraphs: Sentence[][];
}

export interface Evidence {
  id: string;
  title: string;
  url: string;
  content: string;
  task: string;
}

export interface Task {
  id: string;
  question: string;
  depends_on: string[];
  status: string;
  round: number;
}

export interface RunState {
  id: string;
  question: string;
  status: string;
  stage: string;
  error: string | null;
  hold: string | null;
  approve_plan: boolean;
  tasks: Task[];
  findings: Record<string, { summary: string; evidence: string[] }>;
  evidence: Evidence[];
  report: { title: string; sections: ReportSection[] } | null;
  limitations: string[];
  faithfulness: Array<Record<string, number>>;
  replans: number;
  repairs: number;
  delivery: { status: string; error?: string } | null;
  lease: { owner: string; expires_in: number } | null;
  verdict_source?: string;
  thread_id?: string;
  parent_id?: string | null;
  turn?: number;
  mode?: "report" | "followup";
  route?: Route | null;
  metrics: {
    llm: Record<string, Record<string, number>>;
    tools: Record<string, Record<string, number>>;
    stages: Record<string, Record<string, number>>;
    context?: Record<string, Record<string, number>>;
    usage: Record<string, number>;
    derived: Record<string, number | null>;
  };
}

export interface Route {
  decision: "answer" | "extend" | "new";
  question: string;
  tasks: string[];
  missing: string[];
  reason: string;
}

export interface RunSummary {
  id: string;
  question: string;
  status: string;
  created_at?: number;
  thread_id?: string | null;
}

export interface Source {
  demo: boolean;
  listRuns(): Promise<RunSummary[]>;
  getState(id: string): Promise<RunState>;
  /** The runs of one conversation, oldest first. */
  getThread(threadId: string): Promise<RunSummary[]>;
  /** Stream events from the start; returns an unsubscribe function. */
  subscribe(id: string, onEvents: (events: EventRecord[]) => void, onEnd: () => void): () => void;
  submit?(question: string, approvePlan: boolean): Promise<string>;
  followup?(id: string, question: string): Promise<string>;
  control?(id: string, action: "pause" | "cancel" | "resume"): Promise<void>;
  approve?(id: string, tasks: Array<Pick<Task, "id" | "question" | "depends_on">> | null): Promise<void>;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

/** Parse a Server-Sent Events stream read with fetch (EventSource cannot listen to arbitrary event names). */
async function readSse(res: Response, onEvent: (e: EventRecord | { end: string }) => void, signal: AbortSignal) {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (!signal.aborted) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const fields: Record<string, string> = {};
      for (const line of block.split("\n")) {
        const i = line.indexOf(": ");
        if (i > 0) fields[line.slice(0, i)] = line.slice(i + 2);
      }
      if (fields.event === "end") {
        onEvent({ end: JSON.parse(fields.data ?? "{}").status ?? "" });
        return;
      }
      if (fields.event && fields.data) {
        const { ts, ...payload } = JSON.parse(fields.data);
        onEvent({ id: Number(fields.id), ts, kind: fields.event, payload });
      }
    }
  }
}

const FINAL = ["done", "failed", "cancelled"];

export function liveSource(): Source {
  return {
    demo: false,
    listRuns: () => fetch("/runs?limit=50").then(json<RunSummary[]>),
    getState: (id) => fetch(`/runs/${id}/state`).then(json<RunState>),
    getThread: (threadId) => fetch(`/threads/${threadId}`).then(json<RunSummary[]>),
    subscribe(id, onEvents, onEnd) {
      const controller = new AbortController();
      let last = 0;
      let stopped = false;
      const connect = async () => {
        while (!stopped) {
          try {
            const res = await fetch(`/runs/${id}/events?after=${last}`, { signal: controller.signal });
            let ended: string | null = null;
            await readSse(res, (e) => {
              if ("end" in e) {
                ended = e.end;
                return;
              }
              last = e.id;
              onEvents([e]);
            }, controller.signal);
            if (ended !== null) {
              onEnd();
              if (FINAL.includes(ended)) return;
              // Held runs (paused, awaiting approval, halted) can resume later: keep listening slowly.
              await new Promise((r) => setTimeout(r, 3000));
            }
          } catch {
            if (stopped) return;
            await new Promise((r) => setTimeout(r, 1500)); // reconnect, resuming after the last event
          }
        }
      };
      connect();
      return () => {
        stopped = true;
        controller.abort();
      };
    },
    async submit(question, approvePlan) {
      const res = await fetch("/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question, approve_plan: approvePlan }),
      });
      return (await json<{ id: string }>(res)).id;
    },
    async followup(id, question) {
      const res = await fetch(`/runs/${id}/followups`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question }),
      });
      return (await json<{ id: string }>(res)).id;
    },
    async control(id, action) {
      await fetch(`/runs/${id}/${action}`, { method: "POST" }).then(json);
    },
    async approve(id, tasks) {
      await fetch(`/runs/${id}/plan/approve`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(tasks ? { tasks } : {}),
      }).then(json);
    },
  };
}

interface Recording {
  id: string;
  question: string;
  events: EventRecord[];
  state: RunState;
}

/** Replays recorded real runs: events are re-emitted with their original pacing (compressed). */
export function demoSource(): Source {
  const base = import.meta.env.BASE_URL;
  const cache = new Map<string, Promise<Recording>>();
  const load = (id: string) => {
    if (!cache.has(id)) cache.set(id, fetch(`${base}demo/${id}.json`).then(json<Recording>));
    return cache.get(id)!;
  };
  return {
    demo: true,
    listRuns: () => fetch(`${base}demo/index.json`).then(json<RunSummary[]>),
    getState: (id) => load(id).then((r) => r.state),
    getThread: (threadId) =>
      fetch(`${base}demo/index.json`)
        .then(json<RunSummary[]>)
        .then((runs) => runs.filter((r) => (r.thread_id ?? r.id) === threadId)),
    subscribe(id, onEvents, onEnd) {
      let stopped = false;
      load(id).then(async (rec) => {
        const events = rec.events;
        for (let i = 0; i < events.length && !stopped; i++) {
          const gap = i === 0 ? 0 : events[i].ts - events[i - 1].ts;
          await new Promise((r) => setTimeout(r, Math.min(Math.max(gap * 250, 60), 700)));
          if (!stopped) onEvents([events[i]]);
        }
        if (!stopped) onEnd();
      });
      return () => {
        stopped = true;
      };
    },
  };
}

export const source: Source = import.meta.env.VITE_DEMO === "1" ? demoSource() : liveSource();
