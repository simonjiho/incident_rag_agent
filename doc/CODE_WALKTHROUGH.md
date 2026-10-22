# 코드 따라 읽기 — 개선 전(Base)과 개선 후

발표 전에 코드 구조를 잡기 위한 문서. 위에서부터 순서대로 읽으면 된다.

---

## 0. 이 에이전트가 하는 일

사용자가 "ledger-service랑 portal-service 둘 다 장애예요"라고 보내면

1. 그게 잡담인지 장애 신고인지 가른다
2. 어떤 시스템을 조사할지 계획을 세운다
3. 시스템마다 모니터링 수치를 조회한다 (MCP **Tool**)
4. 장애 유형에 맞는 대응 매뉴얼(runbook)을 읽는다 (MCP **Resource**)
5. LLM이 수치와 매뉴얼을 묶어 진단 보고를 쓰고, 형식 검사를 통과하면 답한다

핵심 개선은 **4번에서 "어떤 매뉴얼을 고르느냐"** 를 바꾼 것이다. 그 밖에 5번 뒤에 **사람 승인** 단계를 붙였다(재시작·롤백 같은 조치가 있으면 멈추고 묻는다). 나머지 흐름은 Base와 같다.

---

## 1. 프로그램이 몇 개로 나뉘어 도는가

```
브라우저 (web_chat.html, Vue)
   │  http://localhost:8600
web_demo_server.py        ← 화면 파일을 주고 요청을 백엔드로 넘겨 주는 중계(프록시)
   │  http://localhost:8100
server.py (FastAPI)       ← /chat, /chat/stream (+ 승인 후 재개 /chat/approve, /chat/resume). 서버가 켜질 때 그래프·LLM·MCP 연결을 한 번 만든다
   │
agent/graph.py (LangGraph) ← 노드들을 잇는 흐름
   │
agent/mcp_client.py       ← MCP 서버와 통신 (stdio, 별도 프로세스)
   │
mcp_server.py             ← Tool / Resource / Prompt 제공. data/ 폴더를 읽는다
```

**안 바꾼 것**: server.py의 골격(lifespan에서 한 번 생성, /chat·/chat/stream), 세션 저장(SQLite), 재시도(`retry.py`), v1 legacy 경로.
**덧붙인 것**: 화면(새 진행 단계·승인 카드), 프록시(승인 중계), LLM 백엔드 하나(fake / ollama / openai + **anthropic**).

---

## 2. 개선 전(Base)의 흐름

```
route → plan → gate → research → replan_check ─(runbook 없음)→ research
                                       └─(충분)→ analysis → write ⇄ validate → finalize
```

모든 노드가 `agent/graph.py` **한 파일**(338줄)에 있었다.

| 노드 | 하는 일 |
|---|---|
| route | 잡담이면 `chat_answer`로 바로, 장애면 `plan`으로 |
| plan | MCP Prompt(진단 절차)를 받아 LLM이 task 목록을 만든다. **모니터링 task만** 만든다 |
| gate | `needs_analysis=False`인데 analysis task가 섞여 있으면 코드로 지운다 (권위 게이트) |
| research | task를 병렬로 실행: 모니터링 Tool 호출, runbook Resource 조회 |
| replan_check | runbook을 아직 못 읽었으면 → **키워드 규칙으로 유형 하나를 골라** task를 추가하고 research로 되돌아감 |
| write / validate | LLM이 보고 작성 → "30자 이상 + `1)` 포함"인지 검사, 아니면 다시 작성 (최대 3번) |

### 매뉴얼을 고르던 방식 (문제의 핵심)

`replan_check` 안의 `_classify_incident_type`:

```python
_INCIDENT_TYPE_RULES = [("latency","high-latency"), ("pool","high-latency"), ("cpu","cpu-spike"),
                        ("memory","memory-leak"), ("heap","memory-leak"), ("timeout","network-timeout"),
                        ("network","network-timeout"), ("disk","disk-full"), ("down","db-down")]

text = 모든 시스템의 상태 + 알림 문구를 한 줄로 합쳐 소문자로
for keyword, incident_type in _INCIDENT_TYPE_RULES:
    if keyword in text: return incident_type     # 처음 걸린 하나만
return "db-down"                                  # 아무것도 안 걸리면
```

여기서 문제가 세 개 나온다.

1. 키워드가 없으면 무조건 `db-down` — 예: "TLS cert expired"
2. 엉뚱한 단어에 낚인다 — 예: DB 락의 "Lock wait **timeout**" → `network-timeout`
3. 시스템이 여럿이어도 알림을 합쳐 **매뉴얼을 하나만** 고른다

그리고 plan이 runbook task를 만들지 않으니, **모든 장애 요청이 replan_check에서 되돌아가는 라운드를 탔다**(재계획이 예외가 아니라 정상 경로였다).

---

## 3. 개선 후의 흐름

```
route → plan → gate → monitoring_agent → runbook_agent → replan_check ─(빠진 시스템)→ runbook_agent
                                                                   └─(충분)→ analysis → write ⇄ validate → approval → finalize
                                                                                                        └─(거부 1회)→ write
```

| 노드 | 개선 전 | 개선 후 |
|---|---|---|
| route, gate, analysis, write, validate, finalize | graph.py 안 | 내용 그대로, `agent/nodes/`에 파일 하나씩으로 이동 |
| plan | 시스템명을 틀리면 조사 누락 가능 | 질의에 이름이 나온 시스템은 항상 조사 (§8) |
| research | 노드 하나가 모니터링 + runbook 조회 | **`monitoring_agent`** 서브 에이전트(모니터링만) |
| (없음) | — | **`runbook_agent`** 서브 에이전트: 시스템마다 runbook 선택 → 유형별로 한 번씩 읽기 |
| replan_check | runbook 없으면 키워드로 하나 골라 재조사 | "장애 시스템마다 runbook이 붙었나" 검사. 빠진 시스템만 1회 재시도 |
| (없음) | — | **`approval`** 사람 승인: 최종안에 재시작·롤백 같은 조치가 있으면 멈추고 담당자에게 묻는다 (§8-2) |

`agent/graph.py`는 이제 노드를 잇는 87줄짜리 조립 파일이다.

---

## 4. 요청 하나 따라가기 — "ledger-service랑 portal-service 둘 다 장애예요"

괄호 안은 실행되는 파일과 함수.

**① 요청 받기** (`server.py` → `/chat` → `_handle_chat`)
세션 이력을 불러와 `graph.ainvoke({"query": ..., "history": ..., "events": [], "hitl": ...}, config)`를 부른다. 그래프는 서버가 켜질 때 `lifespan`에서 한 번만 만들어 둔 것이다(싱글턴). `config`에는 요청마다 새로 만든 체크포인트 이름(`run_id`)이 들어간다 — 사람 승인으로 멈췄을 때 이어 가기 위한 것이다.

**② route** (`agent/nodes/route.py`)
fast LLM이 "incident"로 분류 → `plan`으로.

**③ plan** (`agent/nodes/plan.py`)
- MCP 서버에서 `incident_analysis_prompt`(진단 절차)를 받는다. 어느 서버에 있는지 `find_prompt`로 먼저 찾는다.
- fast LLM이 task 목록을 만든다: `[모니터링 ledger-service, 모니터링 portal-service]`.

**④ gate** (`agent/nodes/gate.py`)
필요 없는 analysis task를 코드로 지운다.

**⑤ monitoring_agent** (`agent/agents/monitoring_agent.py` → `collect_metrics`)
- 모니터링 task들을 `asyncio.gather`로 **동시에** 실행한다.
- 각각 MCP Tool `get_monitoring_status`를 `with_retry`로 감싸 부른다. 일시적 timeout이면 간격을 늘려 재시도한다.
- 결과는 상태의 `collected`에 쌓인다:
  `{"source": "monitoring", "data": {"system_name": "ledger-service", "status": "degraded", "alerts": ["Lock wait timeout exceeded ..."], ...}}`

**⑥ runbook_agent — select_runbooks** (`agent/agents/runbook_agent.py`)
- `missing_systems()`: runbook이 필요한데 아직 없는 시스템을 찾는다. 정상(알림 없음)이거나 모니터링에 없는 시스템은 뺀다.
- 시스템마다 동시에 `selector.select(query, 그 시스템의 모니터링 결과)`를 부른다.
- `selector`는 `deps.runbook_selector` — 서버가 환경변수 `RUNBOOK_SELECTOR`(기본 hybrid)로 만들어 넣어 준 객체다 (§6).
- 결과는 `runbook_selections`에 쌓이고, 화면에 `RunbookSelected` 이벤트로 보인다:
  `ledger-service → db-lock (hybrid, 점수 0.71)`, `portal-service → cert-expiry (hybrid, 점수 0.69)`
  1·2위 cosine 차가 0.01보다 작으면 끝에 `— 1·2위 차 0.0007, 근소`가 붙는다(§6).

**⑦ runbook_agent — read_runbooks**
- 고른 유형별로 **한 번씩만** MCP Resource `runbook://incident-response/{유형}`을 읽는다. 같은 유형인 시스템끼리는 묶는다.
- `collected`에 `{"source": "runbook", "incident_type": "db-lock", "systems": ["ledger-service"], "data": 매뉴얼 본문}`이 쌓인다.
- 하나를 못 읽어도 나머지는 계속 읽고, 실패는 `RunbookReadFailed` 이벤트로 남긴다.

**⑧ replan_check** (`agent/nodes/replan_check.py`)
`missing_systems()`가 비어 있으면 충분 → analysis로. 빠진 시스템이 있으면 한 번만 `force_keyword=True`로 runbook_agent를 다시 돌린다(선택 실패한 시스템은 키워드 규칙으로, 조회만 실패한 시스템은 같은 유형을 다시 읽기).

**⑨ write → validate → finalize**
quality LLM이 수치와 매뉴얼로 보고를 쓴다. 매뉴얼마다 "적용 시스템"이 붙어 있어서 답변이 시스템별 절차로 나뉜다. validate가 형식을 검사하고 통과하면 approval로 간다.

**⑩ approval → finalize** (`agent/nodes/approval.py`, 웹 화면에서 "사람 승인"이 켜져 있을 때)
이 예에서는 ledger-service 매뉴얼의 "`pg_terminate_backend`로 세션 종료" 문장이 걸려 그래프가 멈추고 화면에 승인 카드가 뜬다. 승인하면 그대로 finalize, 거부하면 그 조치를 뺀 답변을 다시 쓴다.

---

## 5. RAG는 어떻게 동작하나 (`rag/`, `mcp_server.py`)

### MCP Tool로 만든 이유
질의마다 결과가 달라지는 동적 검색이라 Resource(고정 문서)가 아니라 Tool이다. 에이전트는 `find_tool("search_runbook")`으로 서버를 찾아 부른다. 서버 이름을 코드에 박지 않는다.

```python
@mcp.tool()
def search_runbook(query: str, top_k: int = 3, mode: str = "hybrid") -> RunbookSearchResult:
    return get_index().search(query, top_k=top_k, mode=mode)
```

### 인덱스 만들기 (`rag/index.py` — `parse_chunks`)
runbook마다 `## 증상` 항목을 **한 줄씩 청크**로 쪼갠다(제목 + 증상 한 줄). 그리고 증상 전체를 묶은 청크도 하나 만든다. 14종에서 청크 124개가 나온다.

### 검색 세 가지
| 방식 | 원리 | 파일 |
|---|---|---|
| vector | bge-m3(Ollama)로 질의와 청크를 벡터로 바꿔 cosine 유사도. 벡터를 정규화해 두므로 내적이 곧 cosine | `rag/embedder.py`, `index.py` |
| bm25 | 단어가 얼마나 겹치나. 한글은 형태소 분석 없이 어절 + 글자 두 개씩(bigram)으로 쪼갠다 — "인증서가"와 "인증서"가 "인증", "증서"를 공유 | `rag/bm25.py` |
| hybrid | 두 목록의 **순위만** 써서 합친다(RRF): 점수 = Σ 1/(60 + 순위) | `index.py` — `rrf_scores` |

- runbook 점수 = 그 runbook 청크들 중 **최고점**. 증상 한 줄만 딱 맞아도 그 매뉴얼이 올라온다.
- 검색 질의는 "시스템명 + 상태 + 알림 문장". 사용자 질의는 넣지 않았다. 질의 하나가 여러 시스템을 같이 말하면 다른 시스템 증상이 섞이기 때문이다(측정으로 결정).

### Ollama가 죽으면
- 임베딩 호출이 실패하면 BM25만으로 순위를 내고 `degraded=true`로 알린다.
- 한 번 실패하면 30초 동안은 Ollama를 부르지 않는다(서킷 브레이커). 질의 임베딩은 5초 안에 안 오면 포기한다.
- 청크 임베딩은 `data/.rag_cache/`에 저장해 두고, 서버를 다시 켜도 다시 계산하지 않는다.
- 질의 임베딩은 MCP 서버 프로세스 메모리에 캐시한다(같은 알림 문장은 Ollama를 다시 부르지 않는다). 그래서 Ollama를 꺼도 이미 한 번 검색한 시스템은 그대로 hybrid로 나온다.

---

## 6. 선택기와 폴백 (`agent/runbook_selector.py`)

| 클래스 | 하는 일 |
|---|---|
| `KeywordSelector` | 원래 키워드 규칙을 **시스템 하나에만** 적용 (개선 전 기준선이자 폴백) |
| `McpRagSelector` | `search_runbook`을 불러 1위를 고른다 |

`McpRagSelector.decide()`의 판단 순서:

```
정상 · 모니터링에 없는 시스템 → 고르지 않음
검색 결과 없음               → 키워드 규칙
임베딩 서버 장애(degraded)   → BM25 1위
1위 cosine < 0.52            → 키워드 규칙   (RRF 점수는 순위라 절대값 의미가 없어서 cosine에만 건다)
그 외                        → RAG 1위
```

RAG 1위를 고를 때 `margin`(1위 cosine − 나머지 중 최고 cosine)도 기록한다. 0.01보다 작으면 화면에 "근소"로 표시만 하고 선택은 바꾸지 않는다. e2e 유일한 오답(e03 cache-service, 0.0007)이 여기에 걸린다.

### 의존성 주입 (DI)
- `server.py`의 `lifespan`에서 `build_selector(mcp, RUNBOOK_SELECTOR)`로 선택기를 만든다.
- 그걸 `SupervisorDeps.runbook_selector`에 넣는다(`agent/deps.py`).
- 노드는 `functools.partial(fn, deps=deps)`로 deps를 받는다. runbook_agent는 `deps.runbook_selector.select()`만 부르고, 어떤 선택기인지 모른다.
- 그래서 `RUNBOOK_SELECTOR=keyword`로 켜면 같은 코드로 개선 전 방식을 재현할 수 있고, 테스트에서는 가짜 선택기를 넣을 수 있다.

---

## 7. 서브 에이전트(Multi-Agent)는 어떻게 붙였나

각 서브 에이전트는 **작은 LangGraph를 compile한 것**이다.

```python
def build_runbook_agent(deps):
    g = StateGraph(AgentState)
    g.add_node("select_runbooks", partial(select_runbooks, deps=deps))
    g.add_node("read_runbooks", partial(read_runbooks, deps=deps))
    g.set_entry_point("select_runbooks")
    g.add_edge("select_runbooks", "read_runbooks")
    g.add_edge("read_runbooks", END)
    return g.compile(name="runbook_agent")
```

메인 그래프에서는 이걸 노드 하나처럼 붙인다: `g.add_node("runbook_agent", build_runbook_agent(deps))`.
상태(`AgentState`)를 같이 쓰므로 메인 그래프와 데이터를 주고받는 코드가 따로 없다. LangSmith에서는 서브 에이전트 안쪽이 따로 묶여 보인다(발표 자료 9쪽 — 개선 전·후 트리 비교). `/chat/stream`은 `subgraphs=True`로 스트리밍해서 서브 에이전트 안쪽 노드가 끝날 때마다 화면에 단계가 찍힌다.

---

## 8. 실모델에서 찾아 고친 버그 — 계획 보정

**증상**: qwen3:0.6b로 돌리니 시스템 3개 질의에서 2개가 조사에서 통째로 빠졌다.

**원인** (`agent/ollama_llm.py`, `openai_llm.py`의 `plan()` — Base 코드. Claude 백엔드는 같은 보정을 `anthropic_llm._normalize_plan`에 넣었다):

```python
target = target if target in KNOWN_SYSTEMS else _guess_target(query)  # 이름이 틀리면 → 질의의 첫 시스템으로
if target in seen: continue                                            # 이미 있으면 버림 → 그 시스템 사라짐
```

**수정**: 질의에 이름이 그대로 나온 시스템은 모델이 빠뜨려도 코드가 항상 task를 추가한다. 모델 판단을 코드로 바로잡는다는 점에서 gate(권위 게이트)와 같은 원칙이다. 재현 테스트는 `tests/test_plan_guard.py`.

---

## 8-2. 사람 승인 (Human-in-the-loop)

**왜**: runbook에는 "프로세스를 재시작한다", "직전 릴리스로 롤백한다", "`pg_terminate_backend`로 세션을 끊는다" 같은 조치가 있다. 에이전트가 이걸 그대로 운영자에게 내보내면 검토 없이 실행될 수 있다.

**어떻게** (`agent/nodes/approval.py`):
1. `find_risky_actions(draft)` — 최종안을 문장으로 나눠 위험 조치(재시작·롤백·삭제·강제 종료·kill·failover·`kubectl delete`·`DROP DATABASE`…)가 든 문장을 찾는다. 영어 동사는 단어 단위로만 잡아서 `pod restarted`·`OOMKilled` 같은 알림 인용은 거른다. LLM에게 묻지 않고 코드로 찾는다(gate와 같은 원칙).
2. 있으면 `interrupt({...})` — LangGraph가 그래프를 그 자리에서 멈추고 상태를 체크포인터에 저장한다.
3. 서버가 사람의 답을 `Command(resume={"approved": ..., "comment": ...})`로 넣으면 approval 노드가 처음부터 다시 실행되고, 이번에는 `interrupt()`가 그 답을 돌려준다.
4. 승인 → finalize. 거부 → "이 조치는 승인되지 않았다, 빼고 다시 써라"를 `validation_feedback`에 넣어 write로 한 번 더. 다시 쓴 안도 거부하면 "⚠️ 담당자가 승인하지 않아 보류한 조치"를 붙여 내보낸다(무한 반복 방지).

**서버** (`server.py`): `InMemorySaver` 체크포인터 하나, 요청마다 새 체크포인트 이름(`run_id`). 멈추면 `/chat`은 `status=approval_required` + `run_id`, `/chat/stream`은 `ApprovalRequired` 이벤트를 보낸다. 이어 가기는 `POST /chat/approve`(JSON) 또는 `GET /chat/resume`(SSE, 화면이 쓰는 것). 대화 이력(SQLite)에는 최종 답변이 나온 뒤에만 남긴다.

**테스트**: `tests/test_approval.py` 11개 — 위험 문장 찾기(알림 인용 `restarted`·`OOMKilled`는 제외, `kubectl delete`·재부팅은 포함), hitl 끔이면 안 멈춤, 안전한 답변은 건너뜀, 승인·거부·두 번 거부, 요청끼리 상태가 섞이지 않음.

**실패했을 때**: 재개 중 LLM 오류가 나면 대기 항목을 되돌려 같은 승인 카드로 다시 시도할 수 있다. 승인 대기 중에 같은 대화로 새 질문이 오면 이전 대기는 취소한다(나중에 승인해서 이력 순서가 꼬이지 않게). 화면은 결정 전까지 입력창을 잠근다.

## 8-3. Claude 백엔드 (`agent/anthropic_llm.py`)

`LLM_BACKEND=anthropic`이면 fast·quality 모두 Claude Haiku 5.5를 쓴다. 다른 백엔드와 메서드 이름·인자가 같아서 노드 코드는 하나도 안 바뀌었다(DI 덕분).
- route·plan은 `output_config.format`(json_schema)로 **스키마 자체를 강제**한다. OpenAI JSON 모드는 "JSON이기만 하면" 통과였다. 그래도 시스템 이름 오타는 남을 수 있어서 계획 보정은 그대로 둔다.
- chat·write는 `messages.stream()`으로 받아 토큰마다 화면에 흘린다.
- thinking은 끄고 effort는 low — 분류·요약 위주라 빠른 쪽을 골랐다. 실측: route 2.3s, plan 2.0s, write 3.3s(qwen3:8b는 plan·write 각 44s).
- 비용은 입력·출력 토큰에 실제 단가($0.10/$0.50 per 1M)를 곱한다.
- 키는 `.env`의 `ANTHROPIC_API_KEY`를 SDK가 직접 읽는다.

**결과**: 개선 전 코드도 같은 Haiku로 돌렸다(`eval/base_patch/`, 전송 계층만 교체). 5/17 → 16/17, 지연 중앙값 둘 다 9.2s, 비용 +9%.

---

## 9. 파일 지도

| 구분 | 파일 |
|---|---|
| **새로 만든 것** | `rag/` (index · bm25 · embedder), `agent/runbook_selector.py`, `agent/agents/` (monitoring_agent · runbook_agent), `agent/nodes/` (노드 파일 10개, approval 포함), `agent/anthropic_llm.py`, `agent/state.py`, `agent/rules.py`, `tests/`, `eval/` (`run_eval.py`, `tools/judge_answers.py`, `base_patch/` 등), `sh/run_demo.sh`, `sh/run_agent_anthropic.sh`, `doc/captures/` |
| **고친 것** | `agent/graph.py`(조립만), `mcp_server.py`(+`search_runbook`), `agent/mcp_client.py`(+`find_tool`), `agent/deps.py`(+`runbook_selector`), `web_demo_server.py`(포트 환경변수, 승인 중계), `server.py`(선택기 주입, `/health`·메타데이터, 서브그래프 스트리밍, 승인 대기·재개, Claude 백엔드), `agent/ollama_llm.py`·`openai_llm.py`·`fake_llm.py`(계획 보정, 답변에 적용 시스템 표시), `web_chat.html`(새 단계 표시, '근소' 표시, 승인 카드), `requirements.txt`(+`anthropic`), `.env.example`(+`ANTHROPIC_API_KEY` 자리), `data/` (runbook 14종, 시스템 15개) |
| **그대로** | `agent/retry.py`, `agent/session_store.py`, `agent/supervisor_v1.py`, `mcp_server_docs.py` |

---

## 10. 테스트와 평가는 무엇을 하나

- **`tests/`** (pytest 112개, Ollama 없이 19초): 가짜 MCP · 가짜 LLM을 deps에 주입해서 노드, 선택기 판단 순서, RAG(가짜 임베딩), 그래프 전체 흐름, 계획 보정, 사람 승인을 검사한다.
- **`eval/run_eval.py selector`**: 평가셋 40건으로 선택기만 비교 (키워드 30% · BM25 62.5% · hybrid 82.5% · vector 90%).
- **`eval/run_eval.py e2e`**: 서버에 실제로 12건을 보내 정답 runbook을 읽었는지 센다 (5/17 → 16/17 — fake, qwen3:0.6b, Claude Haiku 모두 같음).
- **`eval/tools/check_data.py`**: 평가 데이터가 runbook 문장을 베끼지 않았는지, 범주가 맞는지 검사한다.
- **`eval/tools/judge_answers.py`**: e2e 결과 파일의 답변을 Sonnet 5.5가 정답 runbook·모니터링 범위 기준으로 5점 채점한다 (Haiku 답변 4.33 → 4.92).
- **`eval/base_patch/`**: 개선 전 코드를 같은 Claude 모델로 돌리기 위한 어댑터(전송 계층만 교체)와 적용법.

---

## 11. 교수님이 물어볼 만한 질문

**Q. RAG를 왜 MCP Tool로 만들었나?**
검색은 질의마다 결과가 달라서 Tool이 맞다. 에이전트 코드에 검색 로직을 넣지 않아 다른 에이전트도 같은 Tool을 쓸 수 있고, 서버는 `find_tool`로 찾아서 이름을 하드코딩하지 않는다.

**Q. hybrid가 vector보다 낮은데 왜 hybrid가 기본인가?**
평가셋 결과를 보고 기본값을 바꾸면 테스트셋에 맞추는 셈이라 그대로 뒀다. 원인은 runbook의 "CPU는 정상" 같은 부정 문장에 BM25가 걸리는 것이고, 한계로 정리했다. `RUNBOOK_SELECTOR=vector`로 바로 바꿀 수 있다.

**Q. RRF가 뭔가?**
두 검색 결과의 점수 대신 순위만 써서 1/(60+순위)를 더한다. 점수 단위가 다른 cosine과 BM25를 섞을 때 쓴다.

**Q. 임계값 0.52는 어떻게 정했나?**
평가셋 앞 10건(dev)만 보고 정답 최저점보다 조금 낮게 잡았다. 40건 중 1건만 폴백해서 효과가 거의 없다 — 한계로 적었다.

**Q. 서브 에이전트로 나누면 뭐가 좋은가?**
모니터링과 매뉴얼 선택이 서로 모르는 채로 바뀔 수 있다. 예를 들어 runbook_agent에 리랭커를 붙여도 메인 그래프는 그대로다. LangSmith에서도 따로 묶여 보여 어디서 시간이 드는지 바로 보인다.

**Q. 의존성 주입을 어디에 썼나?**
선택기를 `SupervisorDeps`에 넣어 노드에 주입했다. 환경변수 하나로 키워드/RAG를 바꾸고, 테스트에서는 가짜 선택기와 가짜 MCP를 넣는다.

**Q. Ollama가 죽으면?**
BM25만으로 계속 고른다(`bm25-degraded`). 한 번 실패하면 30초 동안은 Ollama를 부르지 않아 요청마다 기다리지 않는다.

**Q. 5/17 → 16/17은 공정한 비교인가?**
세 서버 모두 같은 증강 데이터로 돌렸다. 선택기만 바꾼 효과는 6 → 16이다. 늘어난 10쌍은 모두 신규 시스템에서 나왔고, 키워드 규칙 30%는 평가셋을 그렇게 설계한 값이라고 밝혔다.

**Q. 답변 품질은 어떻게 쟀나?**
e2e는 정답 매뉴얼을 읽었는지만 센다. 그래서 같은 Haiku 답변 24건을 Sonnet이 정답 매뉴얼 기준으로 5점 채점했다(어느 버전인지 숨김). 4.33 → 4.92. 개선 전에도 유형은 대부분 맞혔지만 매뉴얼의 구체 절차가 빠졌다. 심사는 1회이고 사람 평가와 대조하지 않았다.

**Q. 개선의 대가는?**
qwen3:0.6b 기준 지연 중앙값 +3.1초, 비용 +58%(프롬프트에 매뉴얼이 여러 개 들어감). Claude Haiku에서는 지연 차이 없음, 비용 +9%. 임베딩 모델(bge-m3 1.2GB)이 하나 더 필요하다.

**Q. 사람 승인은 왜, 어떻게?**
runbook에 재시작·롤백·세션 강제 종료 같은 조치가 있다. 최종안에서 그런 문장을 코드로 찾아 `interrupt()`로 멈추고, 담당자가 승인하면 내보내고 거부하면 그 조치를 빼고 다시 쓴다. 한계는 단어 목록이라 표현이 다르면 놓친다는 것, 대기 상태가 메모리에 있어 서버 재시작 시 사라진다는 것.

**Q. 이 함수 뭐 하는 건가요?** — 자주 짚힐 함수
- `missing_systems()`: runbook이 필요한데 아직 없는 시스템 목록. select와 replan_check가 같이 쓴다.
- `decide()`: 검색 결과를 보고 RAG 1위를 쓸지, 폴백할지 정한다.
- `rrf_scores()`: 순위 합치기.
- `parse_chunks()`: runbook을 증상 한 줄씩 청크로 자른다.
- `collect_metrics()`: 모니터링 Tool을 병렬 + 재시도로 부른다.
- `top_margin()`: RAG 1위 cosine이 나머지보다 얼마나 앞서는지 — '근소' 표시용.
- `find_risky_actions()`: 최종안에서 승인이 필요한 조치 문장을 찾는다.
- `_stream_graph()` (server.py): 그래프 실행을 SSE로 흘리고, 승인으로 멈추면 `ApprovalRequired`를 보낸다. 새 질문과 승인 후 재개가 같이 쓴다.
