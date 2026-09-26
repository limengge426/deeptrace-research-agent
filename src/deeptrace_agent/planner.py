"""Planner: turns a research question (or a list of evidence gaps) into a task DAG."""

from __future__ import annotations

from . import prompts
from .llm import LLM, LLMFormatError, parse_json_object
from .models import Finding, Plan, PlanError, Task


def _parse_tasks(reply: dict, *, round_: int, known: set[str], max_tasks: int) -> list[Task]:
    raw = reply.get("tasks")
    if not isinstance(raw, list) or not raw:
        raise PlanError('reply must contain a non-empty "tasks" list')
    tasks = []
    for item in raw[:max_tasks]:
        if not isinstance(item, dict) or not str(item.get("question", "")).strip():
            raise PlanError(f"malformed task: {item!r}")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            raise PlanError(f"depends_on must be a list in {item!r}")
        tasks.append(
            Task(
                id=str(item.get("id") or f"t{len(tasks) + 1}").strip(),
                question=str(item["question"]).strip(),
                depends_on=[str(d) for d in deps],
                round=round_,
            )
        )
    clash = known & {t.id for t in tasks}
    if clash:
        raise PlanError(f"task ids already used by earlier rounds: {sorted(clash)}")
    return tasks


async def _plan(llm: LLM, system: str, user: str, *, existing: Plan, round_: int, max_tasks: int) -> list[Task]:
    known = {t.id for t in existing.tasks}
    last_error: Exception | None = None
    for _ in range(2):
        prompt = user if last_error is None else f"{user}\n\nYour previous plan was rejected: {last_error}. Fix it."
        reply = await llm.complete(system, prompt, purpose="plan", json_mode=True)
        try:
            tasks = _parse_tasks(parse_json_object(reply.text), round_=round_, known=known, max_tasks=max_tasks)
            Plan(existing.tasks + tasks).validate()
            return tasks
        except (LLMFormatError, PlanError) as exc:
            last_error = exc
    raise PlanError(f"planner failed twice: {last_error}")


async def initial_plan(llm: LLM, question: str, *, max_tasks: int = 5) -> list[Task]:
    system = prompts.PLANNER_SYSTEM.format(max_tasks=max_tasks, existing_note="")
    return await _plan(llm, system, f"Research question: {question}", existing=Plan(), round_=0, max_tasks=max_tasks)


async def replan(
    llm: LLM,
    question: str,
    plan: Plan,
    findings: dict[str, Finding],
    gaps: list[str],
    *,
    round_: int,
    max_tasks: int = 3,
) -> list[Task]:
    """Plan follow-up tasks for evidence gaps; they may depend on already-finished tasks."""
    system = prompts.PLANNER_SYSTEM.format(
        max_tasks=max_tasks,
        existing_note=" or the finished tasks listed by the user",
    )
    done = "\n".join(f"- {tid}: {f.summary[:300]}" for tid, f in findings.items()) or "(none)"
    user = prompts.REPLAN_USER.format(
        question=question,
        done=done,
        gaps="\n".join(f"- {g}" for g in gaps),
        prefix=f"r{round_}_",
    )
    return await _plan(llm, system, user, existing=plan, round_=round_, max_tasks=max_tasks)
