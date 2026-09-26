#!/usr/bin/env bash
# Deployment drill: the docker-compose topology as local processes.
#
#   mock LLM (OpenAI-compatible) + API (no embedded worker) + 2 worker processes
#
# Submits runs over HTTP, SIGKILLs one worker while it is driving runs, and
# checks that every run still finishes exactly once, taken over by the
# surviving worker after the dead worker's lease expires.
#
#   bash evals/deploy_drill.sh            # needs: pip install -e ".[dev]"
set -euo pipefail

cd "$(dirname "$0")/.."
PY=${PYTHON:-python}
RUNS=${RUNS:-4}
TTL=${TTL:-3}
WORK=$(mktemp -d)
export DEEPTRACE_DB="$WORK/runs.db" DEEPTRACE_API_KEY=mock DEEPTRACE_CORPUS=examples/corpus \
       DEEPTRACE_BASE_URL=http://127.0.0.1:9100/v1
PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; wait 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT

wait_for() { for _ in $(seq 1 50); do curl -sf "$1" >/dev/null && return 0; sleep 0.2; done; echo "timeout: $1"; exit 1; }
json() { "$PY" -c "import sys, json; print(json.load(sys.stdin)$1)"; }

"$PY" evals/mock_llm_server.py --port 9100 --delay 0.25 & PIDS+=($!)
"$PY" -m deeptrace_agent serve --port 8765 --no-worker --lease-ttl "$TTL" >"$WORK/api.log" 2>&1 & PIDS+=($!)
wait_for http://127.0.0.1:9100/calls
wait_for http://127.0.0.1:8765/health
"$PY" -m deeptrace_agent worker --lease-ttl "$TTL" >"$WORK/w1.log" 2>&1 & W1=$!; PIDS+=($W1)
"$PY" -m deeptrace_agent worker --lease-ttl "$TTL" >"$WORK/w2.log" 2>&1 & PIDS+=($!)

IDS=()
for i in $(seq 1 "$RUNS"); do
  IDS+=("$(curl -sf -X POST http://127.0.0.1:8765/runs -H 'content-type: application/json' \
            -d "{\"question\": \"Are heat pumps worth it in cold climates? (#$i)\"}" | json '["id"]')")
done
echo "submitted ${#IDS[@]} runs"

# Kill worker 1 once it is in the middle of a run.
for _ in $(seq 1 50); do
  held=$("$PY" -c "
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
print(c.execute('select count(*) from leases where owner like ? and expires_at > ?', (f'%:{sys.argv[2]}:%', time.time())).fetchone()[0])
" "$DEEPTRACE_DB" "$W1" 2>/dev/null || echo 0)
  [ "$held" -gt 0 ] && break
  sleep 0.1
done
echo "SIGKILL worker 1 (pid $W1) while it holds $held lease(s)"
kill -9 "$W1"

start=$(date +%s)
while :; do
  done_count=$(for id in "${IDS[@]}"; do curl -sf "http://127.0.0.1:8765/runs/$id" | json '["status"]'; done | grep -c '^done$' || true)
  [ "$done_count" -eq "${#IDS[@]}" ] && break
  [ $(( $(date +%s) - start )) -gt 60 ] && { echo "FAIL: only $done_count/${#IDS[@]} runs finished"; tail -20 "$WORK"/*.log; exit 1; }
  sleep 0.5
done
echo "all ${#IDS[@]} runs done $(( $(date +%s) - start ))s after the kill"

"$PY" - "$DEEPTRACE_DB" "$W1" "${IDS[@]}" <<'EOF'
import json, sqlite3, sys
db, dead_pid, run_ids = sys.argv[1], sys.argv[2], sys.argv[3:]
conn = sqlite3.connect(db)
taken_over = 0
for rid in run_ids:
    events = [(k, json.loads(p)) for k, p in conn.execute("SELECT kind, payload FROM events WHERE run_id=? ORDER BY id", (rid,))]
    claims = [p for k, p in events if k == "run_claimed"]
    finished = sum(k == "run_finished" for k, _ in events)
    owners = [c["owner"].split(":")[1] for c in claims]
    history = ", ".join(f"pid {o} token {c['token']}" for o, c in zip(owners, claims))
    print(f"  {rid}: claimed by [{history}], finished x{finished}")
    assert finished == 1, "run finished more than once"
    assert claims[-1]["owner"].split(":")[1] != dead_pid, "dead worker cannot have finished a run"
    taken_over += dead_pid in owners[:-1]
print(f"runs taken over from the killed worker: {taken_over}")
assert taken_over >= 1, "the killed worker was not driving any run"
EOF

curl -sf "http://127.0.0.1:8765/runs/${IDS[0]}/report" | grep -q "## Sources"
curl -sfN "http://127.0.0.1:8765/runs/${IDS[0]}/events" | grep -q "^event: end"
echo "PASS: report and SSE stream served for ${IDS[0]}"
