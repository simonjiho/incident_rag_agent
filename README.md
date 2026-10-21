# incident_rag_agent — 장애 진단 에이전트의 runbook 선택을 RAG로

SKALA AI Mini Project · 과제 1번(Base AI Agent 개선).
Base(`all_total_vue`)는 장애 시스템의 모니터링 알림 문구를 **키워드 규칙**으로 읽어 runbook을 하나 골랐다.
알림에 키워드가 없으면 무조건 `db-down`으로 갔고, 시스템 두 개가 서로 다른 장애여도 runbook은 하나만 읽었다.
이 저장소는 그 부분을 **RAG(bge-m3 임베딩 + BM25 hybrid) 기반 시스템별 선택**으로 바꾸고, 같은 평가셋으로 개선 전·후를 잰다.

## 바꾼 것

| 영역 | 개선 전 | 개선 후 |
|---|---|---|
| runbook 선택 | 키워드 9개 규칙, 실패 시 `db-down` | MCP Tool `search_runbook`(임베딩 + BM25, RRF) → 점수가 낮으면 키워드 규칙으로 폴백 |
| 선택 단위 | 요청 전체에서 1개 | **장애 시스템마다** 따로, 정상 시스템은 건너뜀 |
| 데이터 | runbook 6종, 시스템 6개 | runbook 14종(증상 항목 추가), 시스템 15개, 평가셋 52건 |
| 조사 단계 | `research` 노드 하나 | 서브 에이전트 2개 — `monitoring_agent`, `runbook_agent`(컴파일된 서브그래프) |
| 재계획 | runbook이 없으면 항상 1회 | 선택·조회에 실패한 시스템만 1회 재시도 |
| 구조 | `graph.py` 한 파일 | `agent/nodes/`, `agent/agents/`, `agent/rules.py`, `rag/` 로 분리, 선택기는 의존성 주입 |
| 계획 보정 | 소형 모델이 시스템명을 틀리면 그 시스템 조사가 빠짐 | 질의에 이름이 나온 시스템은 항상 조사 (실모델 평가에서 발견) |
| 검증 | 없음 | pytest 87개, 평가 스크립트 `eval/run_eval.py` |

## 결과 (요약)

e2e 12건(정답 (시스템, runbook) 쌍 17개), fake LLM(결정론), 세 서버 모두 같은 증강 데이터.

| 지표 | 개선 전 코드 | 개선 후 + 키워드 규칙 | 개선 후 + hybrid |
|---|---|---|---|
| 정답 runbook을 읽은 쌍 | 5/17 | 6/17 | **16/17** |
| 케이스 정확 일치율 | 0.25 | 0.333 | **0.917** |
| 불필요하게 읽은 runbook | 7 | 9 | 1 |
| 지연 중앙값 (s) | 0.736 | 0.74 | 0.798 |

실모델(qwen3:0.6b + bge-m3, 같은 12건)에서도 정답 runbook 5/17 → 16/17, 요청 오류 0. 대가는 지연 중앙값 +3.1s, 가상 비용 +58%.

runbook 선택기 단독(40건) top-1: 키워드 규칙 30.0% · BM25 62.5% · **hybrid 82.5%** · vector 90.0%.
키워드 규칙의 30%는 평가셋을 "규칙이 약한 경우 70%"로 설계한 값이라 규칙의 실제 정확도가 아니다.
해석·한계(vector가 hybrid보다 높은 이유, 동점 사례, 데이터 합성의 한계)는 [doc/IMPROVEMENTS.md](doc/IMPROVEMENTS.md).

## 실행

```bash
bash sh/setup_venv.sh        # .venv + requirements (최초 1회)
bash sh/setup_ollama.sh      # qwen3:0.6b · qwen3:8b · bge-m3 (있으면 재사용)
cp .env.example .env         # LangSmith 키를 쓰려면 채운다 (.env는 커밋하지 않음)
bash sh/run_agent_ollma.sh   # 백엔드 :8100 (OpenAI는 run_agent_openai.sh, 과금)
bash sh/run_frontend.sh      # 새 터미널에서, http://localhost:8600
```

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `RUNBOOK_SELECTOR` | `hybrid` | `keyword`(개선 전 규칙) / `hybrid` / `vector` / `bm25` |
| `RAG_MIN_SCORE` | `0.52` | RAG top-1 cosine이 이보다 낮으면 키워드 규칙으로 폴백 |
| `RAG_EMBED_MODEL` | `bge-m3` | Ollama 임베딩 모델 |
| `SUPERVISOR_VERSION` | `v2` | `v1`이면 legacy 선형 파이프라인 |

## 테스트 · 평가 재현

```bash
.venv/bin/python -m pytest -q                       # 87 passed, Ollama 불필요
.venv/bin/python eval/tools/check_data.py           # 평가 데이터 정합성·누수 검사
.venv/bin/python eval/run_eval.py selector          # 선택기 단독 (bge-m3 필요)
LLM_BACKEND=fake RUNBOOK_SELECTOR=hybrid .venv/bin/python -m uvicorn server:app --port 8100
.venv/bin/python eval/run_eval.py e2e --base-url http://127.0.0.1:8100 --label hybrid-fake
.venv/bin/python eval/run_eval.py compare eval/results/e2e_*.json
.venv/bin/python eval/tools/search_demo.py                 # MCP search_runbook 직접 호출 (bge-m3 필요)
.venv/bin/python eval/tools/trace_tree.py <run_id> --compact  # LangSmith 트레이스 트리 (.env 키 필요)
```

## 구조 (바뀐 부분)

```
agent/
├── graph.py              # 조립만: route → plan → gate → monitoring_agent → runbook_agent → replan_check → analysis → write → validate → finalize
├── nodes/                # 노드 하나에 파일 하나
├── agents/
│   ├── monitoring_agent.py   # 서브그래프: get_monitoring_status 병렬 + 재시도
│   └── runbook_agent.py      # 서브그래프: 시스템별 선택 → 유형별 1회 조회
├── runbook_selector.py   # KeywordSelector / McpRagSelector, 폴백 판단
├── rules.py              # 개선 전 키워드 규칙 (폴백과 기준선용)
└── state.py
rag/                      # embedder(bge-m3, 디스크 캐시) · bm25 · index(vector/bm25/hybrid RRF)
mcp_server.py             # + Tool search_runbook
eval/                     # 평가셋, run_eval.py, 결과 json
tests/                    # pytest
doc/IMPROVEMENTS.md       # 문제 → 변경, 새 그래프, 설계 이유, 결과, 한계
```

Base의 구성·유스케이스 설명은 [doc/README.md](doc/README.md)(개선 전 기준)에 그대로 있다.
