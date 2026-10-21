#!/usr/bin/env bash
#
# 백엔드(server.py)와 채팅 화면(web_demo_server.py)을 한 번에 띄운다 — 시연·캡처용.
# 사용: bash sh/run_demo.sh [백엔드 포트, 기본 8100] [화면 포트, 기본 8600]
# 백엔드 설정은 환경변수 그대로: LLM_BACKEND=fake|ollama, RUNBOOK_SELECTOR=keyword|hybrid|vector|bm25 …
# 종료하면(Ctrl+C) 백엔드도 같이 내린다.
#
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"
PY="${DEMO_PYTHON:-python3}"
BP="${1:-8100}"; FP="${2:-8600}"

"$PY" -m uvicorn server:app --port "$BP" &
BPID=$!
trap 'kill "$BPID" 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
    curl -s --max-time 2 "http://127.0.0.1:$BP/health" >/dev/null 2>&1 && break
    sleep 1
done
BACKEND_URL="http://127.0.0.1:$BP" FRONTEND_PORT="$FP" "$PY" web_demo_server.py
