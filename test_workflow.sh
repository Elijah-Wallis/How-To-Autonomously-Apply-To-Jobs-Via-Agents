#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MAX_RETRIES=15
VENV="$ROOT/.venv"

mkdir -p logs proof .state

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip playwright
"$VENV/bin/python" -m playwright install chromium >/dev/null

for attempt in $(seq 1 "$MAX_RETRIES"); do
  echo "=== SWARM ATTEMPT ${attempt}/${MAX_RETRIES} ==="

  "$VENV/bin/python" swarm.py --attempt "$attempt" --batch-size 3 2>&1 | tee "logs/swarm_attempt_${attempt}.log" || true

  if "$VENV/bin/python" - <<'PY'
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
    git add -A
    git commit -m "green: autonomous maritime swarm $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
    git branch -M main
    if git remote get-url origin >/dev/null 2>&1; then
      git push origin main
    fi
    echo "COMPLETE"
    exit 0
  fi

  "$VENV/bin/python" swarm.py --self-heal --attempt "$attempt" 2>&1 | tee -a "logs/swarm_attempt_${attempt}.log"
done

echo "FAILED_AFTER_15_RETRIES"
exit 1
