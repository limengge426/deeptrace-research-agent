# Faithfulness and context evaluation

24 questions over 40 pinned Wikipedia articles (local BM25 search). Writer and in-loop judge: `gpt-4o-mini`. Independent grader: `gpt-4o`, labeling every cited claim of every final report against the full text of its evidence.

## End to end

| Config | Claims fully supported (grader) | Unsupported or contradicted | Uncited figures / report | Tokens / run | Report-stage tokens / run | Largest report-stage call (mean / max) | Judge tokens / run | Repairs / run |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| `single` | 76.7% | 3.3% | 0.33 | 11,194 | 3,971 | 2,786 / 3,529 | 0 | 0.42 |
| `sections` | 77.1% | 3.4% | 0.04 | 13,568 | 6,353 | 1,642 / 2,730 | 0 | 0.25 |
| `sections+judge` | 77.1% | 2.2% | 0.00 | 19,654 | 6,952 | 1,620 / 2,282 | 5,528 | 0.54 |

## Can the in-loop judge catch injected faults?

Faults are injected into claims the grader labeled *supported*; only faults the grader confirms as unsupported or contradicted are kept as ground truth.

| Fault | Confirmed cases | Caught (unsupported/contradicted) | Caught incl. *partial* |
|---|--:|--:|--:|
| Citation swapped to unrelated evidence | 0 | – | – |
| Minimal factual edit (number, entity, direction) | 0 | – | – |

False alarms on untouched supported claims: – (0 claims).
