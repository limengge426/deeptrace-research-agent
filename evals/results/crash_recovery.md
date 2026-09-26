# Crash-recovery benchmark

Uncrashed baseline: 12 LLM calls, 8 searches, 12 checkpoints (with 1 report repair and 1 replan).
Each crash is a SIGKILL of the worker process; recovery runs in a fresh process that waits for the dead process's lease to expire (TTL 0.3s) and takes the run over.

## By injection point

| Scenario | Crashed runs | Recovered | Report identical to uncrashed run | Runs with duplicate searches | Re-executed LLM calls / run |
|---|--:|--:|--:|--:|--:|
| llm | 12 | 12/12 | 12/12 | 0 (0 total) | 0.4 |
| search | 8 | 8/8 | 8/8 | 0 (0 total) | 1.5 |
| search-post | 8 | 8/8 | 8/8 | 8 (8 total) | 1.5 |
| checkpoint | 12 | 12/12 | 12/12 | 0 (0 total) | 1.0 |
| **all single crashes** | 40 | 40/40 | 40/40 | 8 (8 total) | 1.0 |
| **2-3 crashes per run (random)** | 30 | 30/30 | 30/30 | 14 (17 total) | 3.2 |

## By pipeline stage at crash time

| Scenario | Crashed runs | Recovered | Report identical to uncrashed run | Runs with duplicate searches | Re-executed LLM calls / run |
|---|--:|--:|--:|--:|--:|
| plan | 2 | 2/2 | 2/2 | 0 (0 total) | 0.5 |
| execute | 28 | 28/28 | 28/28 | 8 (8 total) | 1.3 |
| report | 6 | 6/6 | 6/6 | 0 (0 total) | 0.5 |
| verify | 4 | 4/4 | 4/4 | 0 (0 total) | 0.2 |

_71 scenarios in 61.5s._
