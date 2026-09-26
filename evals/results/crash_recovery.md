# Crash-recovery benchmark

Uncrashed baseline: 21 LLM calls, 8 searches, 13 checkpoints (with 1 report repair and 1 replan).
Each crash is a SIGKILL of the worker process; recovery runs in a fresh process that waits for the dead process's lease to expire (TTL 0.3s) and takes the run over.

## By injection point

| Scenario | Crashed runs | Recovered | Report identical to uncrashed run | Runs with duplicate searches | Runs with a webhook re-send | Delivered exactly once (after dedup) | Re-executed LLM calls / run |
|---|--:|--:|--:|--:|--:|--:|--:|
| llm | 21 | 21/21 | 21/21 | 0 (0 total) | 0 | 21/21 | 1.1 |
| search | 8 | 8/8 | 8/8 | 0 (0 total) | 0 | 8/8 | 1.5 |
| search-post | 8 | 8/8 | 8/8 | 8 (8 total) | 0 | 8/8 | 1.5 |
| checkpoint | 13 | 13/13 | 13/13 | 0 (0 total) | 0 | 13/13 | 1.6 |
| webhook | 1 | 1/1 | 1/1 | 0 (0 total) | 0 | 1/1 | 0.0 |
| webhook-post | 1 | 1/1 | 1/1 | 0 (0 total) | 1 | 1/1 | 0.0 |
| **all single crashes** | 52 | 52/52 | 52/52 | 8 (8 total) | 1 | 52/52 | 1.3 |
| **2-3 crashes per run (random)** | 30 | 30/30 | 30/30 | 5 (5 total) | 9 | 30/30 | 2.0 |

## By pipeline stage at crash time

| Scenario | Crashed runs | Recovered | Report identical to uncrashed run | Runs with duplicate searches | Runs with a webhook re-send | Delivered exactly once (after dedup) | Re-executed LLM calls / run |
|---|--:|--:|--:|--:|--:|--:|--:|
| plan | 2 | 2/2 | 2/2 | 0 (0 total) | 0 | 2/2 | 0.5 |
| execute | 28 | 28/28 | 28/28 | 8 (8 total) | 0 | 28/28 | 1.3 |
| report | 14 | 14/14 | 14/14 | 0 (0 total) | 0 | 14/14 | 2.0 |
| verify | 5 | 5/5 | 5/5 | 0 (0 total) | 0 | 5/5 | 0.6 |
| deliver | 3 | 3/3 | 3/3 | 0 (0 total) | 1 | 3/3 | 0.0 |

_83 scenarios in 60.0s._
