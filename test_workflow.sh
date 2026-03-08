#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MAX_RETRIES=15
VENV="$ROOT/.venv"
MOCK_PORT="${MOCK_PORT:-8765}"
MOCK_TARGETS="$ROOT/mock_targets.json"
MOCK_LOG="$ROOT/logs/mock_ats.log"
PYTHON_BIN=""
PIP_BIN=()

mkdir -p logs proof .state

cleanup() {
  if [ -n "${MOCK_PID:-}" ] && kill -0 "$MOCK_PID" >/dev/null 2>&1; then
    kill "$MOCK_PID" >/dev/null 2>&1 || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" >/dev/null 2>&1 || true
fi

if [ -x "$VENV/bin/python" ] && [ -x "$VENV/bin/pip" ]; then
  PYTHON_BIN="$VENV/bin/python"
  PIP_BIN=("$VENV/bin/pip")
  "${PIP_BIN[@]}" install --quiet --upgrade pip playwright
else
  PYTHON_BIN="python3"
  PIP_BIN=("python3" "-m" "pip")
  "${PIP_BIN[@]}" install --user --quiet --upgrade playwright
fi

"$PYTHON_BIN" -m playwright install chromium >/dev/null

"$PYTHON_BIN" scripts/mock_maritime_ats.py --host 127.0.0.1 --port "$MOCK_PORT" --targets-out "$MOCK_TARGETS" >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!

"$PYTHON_BIN" - <<PY
import sys
import time
import urllib.request

url = "http://127.0.0.1:${MOCK_PORT}/healthz"
deadline = time.time() + 20
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.read().decode().strip() == "ok":
                print("mock server ready")
                sys.exit(0)
    except Exception:
        time.sleep(0.25)
print("mock server failed to start")
sys.exit(1)
PY

for attempt in $(seq 1 "$MAX_RETRIES"); do
  echo "=== SWARM ATTEMPT ${attempt}/${MAX_RETRIES} ==="

  "$PYTHON_BIN" swarm.py --attempt "$attempt" --batch-size 3 --targets-file "$MOCK_TARGETS" --ttl-seconds 45 2>&1 | tee "logs/swarm_attempt_${attempt}.log" || true

  if "$PYTHON_BIN" - <<'PY'
import json
import pathlib
import sys

allowed = {
    "thank you",
    "application submitted",
    "confirmation",
    "application received",
}

path = pathlib.Path("targets.json")
if not path.exists():
    print("targets.json missing")
    sys.exit(1)

payload = json.loads(path.read_text(encoding="utf-8"))
results = payload.get("results", [])
if len(results) != 10:
    print(f"expected 10 targets, found {len(results)}")
    sys.exit(1)

incomplete = []
for r in results:
    company = r.get("company", "unknown")
    status = r.get("status", "")
    proof = r.get("proof", {}) if isinstance(r.get("proof", {}), dict) else {}

    # BLOCKED is an acceptable terminal state (captcha, dead domain, SMS)
    if status == "BLOCKED":
        continue

    text_hits = {str(x).strip().lower() for x in proof.get("text_hits", [])}
    url_ok = bool(proof.get("url_match", False))
    shot = str(proof.get("screenshot", "")).strip()
    shot_path = pathlib.Path(shot)
    shot_ok = bool(shot) and shot.endswith("_success.png") and shot_path.exists()
    text_ok = any(marker in text_hits for marker in allowed)

    if not (status == "COMPLETE" and (text_ok or url_ok or shot_ok)):
        incomplete.append(company)

if incomplete:
    print("INCOMPLETE:", ", ".join(incomplete))
    sys.exit(1)

print("COMPLETE")
sys.exit(0)
PY
  then
    echo "COMPLETE"
    exit 0
  fi

  "$PYTHON_BIN" swarm.py --self-heal --attempt "$attempt" --targets-file "$MOCK_TARGETS" --ttl-seconds 45 2>&1 | tee -a "logs/swarm_attempt_${attempt}.log"
done

echo "FAILED_AFTER_15_RETRIES"
exit 1
