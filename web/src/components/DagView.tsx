import { layers, type DagTask } from "../derive";

const W = 230;
const H = 78;
const GAP_X = 56;
const GAP_Y = 14;

const STATE_TEXT: Record<string, string> = {
  pending: "queued",
  running: "researching…",
  done: "done",
  failed: "failed",
};

export function DagView({ tasks }: { tasks: DagTask[] }) {
  if (tasks.length === 0) return <div className="empty">Waiting for the planner…</div>;
  const cols = layers(tasks);
  const pos = new Map<string, { x: number; y: number }>();
  cols.forEach((col, c) => col.forEach((t, r) => pos.set(t.id, { x: c * (W + GAP_X), y: r * (H + GAP_Y) })));
  const width = cols.length * (W + GAP_X) - GAP_X;
  const height = Math.max(...cols.map((c) => c.length)) * (H + GAP_Y) - GAP_Y;

  return (
    <div className="dag-scroll">
      <div className="dag" style={{ width, height }}>
        <svg width={width} height={height} className="dag-edges" aria-hidden>
          {tasks.flatMap((t) =>
            t.deps
              .filter((d) => pos.has(d))
              .map((d) => {
                const a = pos.get(d)!;
                const b = pos.get(t.id)!;
                const x1 = a.x + W;
                const y1 = a.y + H / 2;
                const x2 = b.x;
                const y2 = b.y + H / 2;
                const mx = (x1 + x2) / 2;
                return <path key={`${d}-${t.id}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} />;
              }),
          )}
        </svg>
        {tasks.map((t) => {
          const p = pos.get(t.id)!;
          return (
            <div key={t.id} className={`node ${t.state}`} style={{ left: p.x, top: p.y, width: W, height: H }}
                 title={t.question}>
              <div className="node-head">
                <span className="node-id">{t.id}</span>
                {t.round > 0 && <span className="tag">replan</span>}
                <span className="node-state">{STATE_TEXT[t.state]}{t.state === "done" ? ` · ${t.evidence} sources` : ""}</span>
              </div>
              <div className="node-q">{t.question}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
