# Faithfulness and context evaluation

24 questions over 40 pinned Wikipedia articles (local BM25 search). Writer and in-loop judge: `gpt-4o-mini`. Independent grader: `gpt-4o`, labeling every cited claim of every final report against the full text of its evidence.

## End to end

| Config | Claims fully supported (grader) | Unsupported or contradicted | Uncited figures / report | Tokens / run | Report-stage tokens / run | Largest report-stage call (mean / max) | Judge tokens / run | Repairs / run |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| `single` | 76.7% | 3.3% | 0.33 | 11,194 | 3,971 | 2,786 / 3,529 | 0 | 0.42 |
| `sections` | 77.1% | 3.4% | 0.04 | 13,568 | 6,353 | 1,642 / 2,730 | 0 | 0.25 |
| `sections+judge` | 77.1% | 2.2% | 0.00 | 19,654 | 6,952 | 1,620 / 2,282 | 5,528 | 0.54 |

Sectioned writing made the largest report-stage call smaller for 24/24 questions (median reduction 40.6%), at the cost of more calls in total.

Difference in the share of unsupported or contradicted claims (paired bootstrap by question, 95% CI):

- `sections+judge` vs `single`: -1.1 points (-5.3 to +4.0)
- `sections+judge` vs `sections`: -1.2 points (-5.5 to +4.0)

## Can the in-loop judge catch injected faults?

Faults are injected into claims the grader labeled *supported*; only faults the grader confirms as unsupported or contradicted are kept as ground truth.

| Fault | Confirmed cases | Caught as unsupported/contradicted (95% CI) | Flagged incl. *partial* (95% CI) |
|---|--:|--:|--:|
| Citation swapped to unrelated evidence | 132 | 131 = 99.2% (95.8–99.9) | 131 = 99.2% (95.8–99.9) |
| Minimal factual edit (number, entity, direction) | 54 | 40 = 74.1% (61.1–83.9) | 54 = 100.0% (93.4–100.0) |

On 129 untouched claims the grader confirmed as supported, the judge raised 0.0% (0.0–2.9) false alarms (unsupported/contradicted) and flagged 2.3% (0.8–6.6) as *partial*.
