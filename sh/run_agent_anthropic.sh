#!/usr/bin/env bash
#
# Claude API(기본 claude-haiku-5-5)로 에이전트 백엔드를 띄운다 (:8100).
# 실제 과금 호출이다 — Haiku 5.5 기준 장애 진단 1건이 약 0.1센트.
# runbook 검색(bge-m3)은 로컬 Ollama를 쓰므로 Ollama가 떠 있어야 RAG가 정상 동작한다
# (없으면 BM25 순위로 내려간다).
#
# 화면: 새 터미널에서 bash sh/run_frontend.sh → http://localhost:8600
# 종료: Ctrl+C
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"

if [ ! -f .env ]; then
    echo "ERROR: .env가 없습니다 — cp .env.example .env 후 ANTHROPIC_API_KEY를 채우세요."
    exit 1
fi
set -a; source .env; set +a
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: .env에 ANTHROPIC_API_KEY가 비어 있습니다."
    exit 1
fi

echo "모델: fast=${ANTHROPIC_FAST_MODEL:-claude-haiku-5-5} / quality=${ANTHROPIC_QUALITY_MODEL:-claude-haiku-5-5}"
lsof -ti:8100 | xargs kill -9 2>/dev/null || true
LLM_BACKEND=anthropic SUPERVISOR_VERSION=v2 exec python3 -m uvicorn server:app --host 127.0.0.1 --port 8100 --log-level warning
