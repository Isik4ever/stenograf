#!/bin/zsh
# Startar Stenograf på http://127.0.0.1:8790 och öppnar webbläsaren.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH"
PORT="${STENOGRAF_PORT:-8790}"
if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  open "http://127.0.0.1:$PORT"
  exit 0
fi
(sleep 1.5; open "http://127.0.0.1:$PORT") >/dev/null 2>&1 &
exec .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port "$PORT" --log-level warning
