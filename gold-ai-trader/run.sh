#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "No .env found. Run ./setup.sh first." >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "AURUM AI needs Python 3.9 or newer." >&2
  exit 1
fi

exec "$PYTHON" -m app.main
