#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

./test_workflow.sh

git add -A
git commit -m "green: autonomous maritime swarm $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
git branch -M main
if git remote get-url origin >/dev/null 2>&1; then
  git push origin main
fi
