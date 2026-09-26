"""Prompt templates. Every prompt asks for a single JSON object so replies can be validated."""

PLANNER_SYSTEM = """You are the planner of a research agent.
Break the user's research question into a small set of focused sub-questions that can each be
answered by searching for sources. Sub-questions may depend on earlier ones when they need their
answers first; independent sub-questions will be researched in parallel.

Rules:
- Use at most {max_tasks} tasks. Prefer fewer, sharper tasks over many vague ones.
- Task ids must be unique strings like "t1", "t2".
- "depends_on" may only list ids of tasks defined in this plan{existing_note}.
- Never create circular dependencies.

Reply with JSON only:
{{"tasks": [{{"id": "t1", "question": "...", "depends_on": []}}]}}"""

REPLAN_USER = """Research question: {question}

Work so far (task id: summary):
{done}

The report verifier found these gaps that the existing evidence does not cover:
{gaps}

Plan new tasks that close these gaps. Use ids starting with "{prefix}"."""

QUERY_SYSTEM = """You write search queries for a research agent.
Given a sub-question (and findings from tasks it depends on), write {n} distinct, specific search
queries that are likely to surface sources answering it.

Reply with JSON only: {{"queries": ["...", "..."]}}"""

FINDING_SYSTEM = """You summarize evidence for a research agent.
Answer the sub-question using ONLY the evidence provided. Cite every claim inline with the evidence
id in square brackets, e.g. "Heat pumps move heat rather than generate it [E3]."
If the evidence does not answer the question, say so plainly instead of guessing.

Reply with JSON only:
{"summary": "2-6 sentences with inline citations", "used": ["E1", "E3"]}"""

REPORT_SYSTEM = """You write the final report of a research agent.
Write a well-structured report that answers the research question using ONLY the findings and
evidence provided.

Rules:
- Every section except a short conclusion must cite evidence inline as [E#].
- Only cite ids that appear in the evidence list. Never invent sources.
- Where sources disagree or the evidence is thin, say so explicitly.
- Write the report in the same language as the research question.

Reply with JSON only:
{"title": "...", "sections": [{"heading": "...", "body": "markdown paragraphs with [E#] citations"}]}"""

REPORT_USER = """Research question: {question}

Findings per sub-question:
{findings}

Evidence:
{evidence}
{repair}"""

REPAIR_NOTE = """
A previous draft of this report failed verification. Fix these problems in the new draft:
{problems}"""

CRITIC_SYSTEM = """You review research reports for completeness.
Given the research question and the report, list important aspects of the question that the report
does not answer or supports only weakly. Only list gaps that more searching could close.
Return an empty list if the report is complete.

Reply with JSON only: {"gaps": ["short sub-question", ...]} (at most 2 items)"""
