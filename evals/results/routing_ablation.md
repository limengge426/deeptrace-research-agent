# Evidence routing ablation

24 questions, pinned Wikipedia corpus, `gpt-4o-mini`, sectioned reports, claim judge off. `routed`: each section sees only its findings' evidence (DeepTrace). `unrouted`: every section sees all evidence. Quality graded by `gpt-4o`.

| | routed | unrouted | Reduction (95% CI, paired bootstrap) | Questions where routed is smaller |
|---|--:|--:|--:|--:|
| LLM tokens per run | 13,146 | 16,403 | **19.9%** (13.3% to 25.7%) | 20/24 |
| Report-stage tokens per run | 5,836 | 9,271 | **37.0%** (28.7% to 44.0%) | 20/24 |
| Evidence characters placed into report-stage prompts (cumulative exposure) | 10,178 | 26,489 | **61.6%** (54.7% to 66.9%) | 23/24 |
| Evidence characters in the largest report-stage prompt | 4,033 | 7,209 | **44.1%** (35.0% to 52.1%) | 21/24 |
| Findings cited by the final report | 100.0% | 100.0% | | |
| Evidence used by findings that the report cites | 95.7% | 96.4% | | |
| Claims fully supported (grader) | 76.7% | 82.4% | | |
| Claims unsupported or contradicted (grader) | 2.4% | 3.2% | | |

Difference in fully supported claims, routed minus unrouted: -5.6 points (95% CI -9.4 to -0.9).
