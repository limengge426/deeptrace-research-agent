const TONES: Record<string, string> = {
  done: "good",
  failed: "bad",
  cancelled: "bad",
  halted: "warn",
  paused: "warn",
  awaiting_approval: "warn",
  needs_reconciliation: "bad",
};

const LABELS: Record<string, string> = {
  awaiting_approval: "awaiting approval",
  needs_reconciliation: "needs reconciliation",
};

export function StatusPill({ status, small }: { status: string; small?: boolean }) {
  const tone = TONES[status] ?? "active";
  return <span className={`pill ${tone} ${small ? "small" : ""}`}>{LABELS[status] ?? status}</span>;
}
