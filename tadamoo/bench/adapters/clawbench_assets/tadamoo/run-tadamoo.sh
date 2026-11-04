#!/bin/bash
set -e

# Run-time script of the TaDaMoo harness: waits for the browser, runs the harness against it, watches the time and the interceptor, and leaves the files
# ClawBench collects (/data/agent-messages.jsonl, /data/usage.jsonl, /data/.stop-reason).
/setup-tadamoo.sh || { mkdir -p /data; echo "tadamoo_failed" > /data/.stop-reason; exit 1; }

mkdir -p /data /root/workspace
: > /data/agent-messages.jsonl
: > /data/usage.jsonl

echo "Waiting for Chrome CDP..."
for i in $(seq 1 30); do
  if curl -sf "${CLAWBENCH_BROWSER_CDP_URL%/}/json/version" > /dev/null 2>&1; then
    echo "Chrome CDP ready"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "Chrome CDP not ready after 30s, aborting"
    echo "chrome_cdp_timeout" > /data/.stop-reason
    exit 1
  fi
  sleep 1
done

cd /root/workspace
python3 /tadamoo_driver.py > /tmp/tadamoo-stdout.log 2> /tmp/tadamoo-stderr.log &
AGENT_PID=$!
sleep 3
if ! kill -0 $AGENT_PID 2>/dev/null && [ ! -s /data/usage.jsonl ] && ! grep -q '"type": "stop"' /data/agent-messages.jsonl 2>/dev/null; then
  echo "TaDaMoo driver died on startup."
  echo "tadamoo_failed" > /data/.stop-reason
fi

# Watchdog: the interceptor matched, the agent stopped acting for 300 s, or the time limit.
IDLE_THRESHOLD=300
MAX_WAIT=${TIME_LIMIT_S:-1800}
ELAPSED=0
LAST_SIZE=0
IDLE=0
STOP_REASON=""

while kill -0 $AGENT_PID 2>/dev/null && [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
  sleep 5
  ELAPSED=$((ELAPSED + 5))

  if [ -f /data/.stop-requested ]; then
    echo "Stop requested by server (eval matched)."
    STOP_REASON="eval_matched"
    break
  fi

  CURRENT_SIZE=$(wc -c < /data/actions.jsonl 2>/dev/null || echo 0)
  if [ "$CURRENT_SIZE" -gt 0 ] && [ "$CURRENT_SIZE" -eq "$LAST_SIZE" ]; then
    IDLE=$((IDLE + 5))
    if [ "$IDLE" -ge "$IDLE_THRESHOLD" ]; then
      echo "Agent idle for ${IDLE_THRESHOLD}s, assuming done."
      STOP_REASON="agent_idle"
      break
    fi
  else
    IDLE=0
  fi
  LAST_SIZE=$CURRENT_SIZE
done

if [ -z "$STOP_REASON" ]; then
  if [ -f /data/.stop-reason ]; then
    STOP_REASON=$(cat /data/.stop-reason)
  elif ! kill -0 $AGENT_PID 2>/dev/null; then
    STOP_REASON="agent_exited"
  else
    echo "Time limit (${MAX_WAIT}s) exceeded, killing agent."
    STOP_REASON="time_limit_exceeded"
  fi
fi
echo "$STOP_REASON" > /data/.stop-reason

# The driver ends the trajectory itself when it is allowed to finish; asked to stop, it is ended here after a moment to write its files.
if kill -0 $AGENT_PID 2>/dev/null; then
  kill -INT $AGENT_PID 2>/dev/null || true
  for _ in $(seq 1 20); do kill -0 $AGENT_PID 2>/dev/null || break; sleep 0.5; done
  kill -KILL $AGENT_PID 2>/dev/null || true
fi
if ! grep -q '"type": "stop"' /data/agent-messages.jsonl 2>/dev/null; then
  python3 - "$STOP_REASON" <<'PYEOF'
import json, sys
reason = {"time_limit_exceeded": "timeout", "agent_idle": "gave_up"}.get(sys.argv[1], sys.argv[1])
with open("/data/agent-messages.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"type": "stop", "reason": reason}) + "\n")
PYEOF
fi
python3 /usage-emitter.py || true
cp /tmp/tadamoo-stdout.log /data/agent-stdout.log 2>/dev/null || true
cp /tmp/tadamoo-stderr.log /data/agent-stderr.log 2>/dev/null || true

curl -sf -X POST http://localhost:7878/api/stop || true
rm -f /data/.stop-requested

echo "Agent finished, recording grace period (15s)..."
sleep 15
echo "Stopping recording..."
curl -sf -X POST http://localhost:7878/api/stop-recording || true
sleep 2
rm -f /data/*.log
echo "Done."
