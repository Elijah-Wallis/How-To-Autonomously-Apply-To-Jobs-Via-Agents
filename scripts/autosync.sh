#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKUP_VOLUME="${SWARM_BACKUP_VOLUME:-/Volumes/maritime-swarm-sync}"
PROJECT_NAME="$(basename "$ROOT")"
TARGET_ROOT="${BACKUP_VOLUME}/${PROJECT_NAME}"

mkdir -p "${TARGET_ROOT}/current" "${TARGET_ROOT}/git" "${TARGET_ROOT}/snapshots"

while true; do
  TS="$(date -u +%Y%m%dT%H%M%SZ)"

  if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    git add -A
    git commit -m "autosync:${TS}" >/dev/null 2>&1 || true
  fi

  rsync -a --delete --exclude ".venv" --exclude "__pycache__" "$ROOT/" "${TARGET_ROOT}/current/"
  rsync -a --delete "$ROOT/.git/" "${TARGET_ROOT}/git/"
  mkdir -p "${TARGET_ROOT}/snapshots/${TS}"
  rsync -a --exclude ".venv" --exclude "__pycache__" "$ROOT/" "${TARGET_ROOT}/snapshots/${TS}/"

  sleep 60
done
