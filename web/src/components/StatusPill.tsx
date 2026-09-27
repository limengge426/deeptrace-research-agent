import { label } from "../i18n";

const TONES: Record<string, string> = {
  done: "good",
  failed: "bad",
  cancelled: "bad",
  halted: "warn",
  paused: "warn",
  awaiting_approval: "warn",
  needs_reconciliation: "bad",
};

export function StatusPill({ status, small }: { status: string; small?: boolean }) {
  const tone = TONES[status] ?? "active";
  return <span className={`pill ${tone} ${small ? "small" : ""}`}>{label("status", status)}</span>;
}
