"""JSON views of a run for the web console (served by the API and exported for demo replays)."""

from __future__ import annotations

from typing import Any

from . import metrics as run_metrics
from .faithfulness import annotate_report
from .store import RunStore


def run_state(store: RunStore, run_id: str) -> dict[str, Any]:
    """Everything the web console shows for one run: plan, evidence, annotated report, metrics."""
    state = store.load(run_id)
    holder = store.lease_holder(run_id)
    return {
        "id": run_id,
        "question": state.question,
        "status": store.status(run_id),
        "stage": state.stage,
        "error": state.error,
        "hold": state.hold,
        "approve_plan": state.approve_plan,
        "tasks": [{"id": t.id, "question": t.question, "depends_on": t.depends_on, "status": t.status,
                   "round": t.round} for t in state.plan.tasks],
        "findings": {tid: {"summary": f.summary, "evidence": f.evidence_ids} for tid, f in state.findings.items()},
        "evidence": [{"id": e.id, "title": e.title, "url": e.url, "content": e.content, "task": e.task_id}
                     for e in state.evidence],
        "report": {"title": state.report.title, "sections": annotate_report(state.report, state.verdicts)}
        if state.report else None,
        # Unknown citations are stripped and partial claims are minor: neither is listed as a limitation.
        "limitations": [i.note or i.detail for i in state.issues
                        if i.code not in ("unknown_citation", "partial_claim")] if state.stage == "done" else [],
        "faithfulness": state.faithfulness,
        "replans": state.replans,
        "repairs": state.repairs,
        "delivery": state.delivery,
        "lease": {"owner": holder[0], "expires_in": round(holder[1], 1)} if holder else None,
        "metrics": run_metrics.run_metrics(state),
    }
