# 평가 데이터 설명 (Build-A: 데이터 증강)

RAG runbook 선택기를 개선 전(키워드 규칙)과 비교하려고 만든 데이터다.
검증은 `.venv/bin/python eval/tools/check_data.py` 한 번으로 끝난다 (통과하면 `PASS`).

## 1. 무엇을 만들었나

| 파일 | 내용 | 개수 |
|---|---|---|
| `data/runbooks/*.md` | 기존 6종을 `## 증상` / `## 조치 절차` 구조로 재작성 + 신규 8종 추가 | 14종 |
| `data/monitoring.json` | 기존 6개 그대로 + 신규 시스템 9개 | 15개 |
| `eval/selector_cases.jsonl` | runbook 선택기 단독 평가 (입력: status, alerts, query) | 40건 |
| `eval/e2e_cases.jsonl` | 에이전트 전체 흐름 평가 (시스템별 정답 유형) | 12건 |
| `eval/tools/check_data.py` | 위 데이터의 형식·분포·누수·규칙 sanity 검사 | - |

### runbook
- 기존 6종(db-down, cpu-spike, disk-full, high-latency, memory-leak, network-timeout)은 **제목 줄과 번호 단계를 원문 그대로** 두고 증상 bullet 7~8개만 앞에 붙였다. `check_data.py`가 개선 전 원본(`all_total_vue_student`)과 글자 단위로 비교한다.
- 증상 bullet에 모니터링 알림 값을 그대로 옮긴 예시 2개(thread-pool-exhaustion의 `queue 200/200`, memory-leak의 `exit code 137`)는 리뷰 후 지웠다. 그 뒤 selector에서 bm25만 26 → 25로 1건(s01) 떨어졌다.
- 신규 8종: cert-expiry, dns-failure, db-lock, queue-backlog, rate-limit, config-error, thread-pool-exhaustion, replication-lag. 증상 bullet 8개, 조치 5단계, 1단계는 `get_monitoring_status` 언급.
- 증상 bullet은 헷갈리는 쌍을 가르도록 썼다: db-lock vs db-down vs network-timeout, thread-pool-exhaustion vs high-latency vs cpu-spike, replication-lag vs db-down, dns-failure vs network-timeout, rate-limit vs network-timeout, config-error vs db-down, memory-leak(OOMKilled) vs cpu-spike. 예를 들면 "DB는 접속·응답이 정상이고 특정 테이블 쓰기만 멈춘다 (DB 다운과 구분)" 같은 식이다.
- fake LLM의 번호 줄 파서(`^\d+\.\s+`)가 14종 모두에서 단계를 정상 추출하는 것까지 확인했다.

### monitoring.json 신규 시스템 9개와 정답 유형

| 시스템 | 정답 유형 | 알림 요약 | 기존 규칙 예측 |
|---|---|---|---|
| ledger-service | db-lock | Lock wait timeout exceeded | network-timeout (`timeout`에 낚임) |
| portal-service | cert-expiry | TLS cert expired, 상태 `down` (유일한 down) | db-down (상태 `down`에 낚임) |
| search-service | dns-failure | getaddrinfo ENOTFOUND | db-down (키워드 없음, 폴백) |
| notification-service | queue-backlog | consumer lag 480k | db-down (폴백) |
| shipping-service | rate-limit | HTTP 429, quota exhausted | db-down (폴백) |
| invoice-service | config-error | CrashLoopBackOff, 환경값 비어 있음 | db-down (폴백) |
| inventory-service | thread-pool-exhaustion | thread pool 100% busy, queue 200/200 | high-latency (`pool`에 낚임) |
| reporting-service | replication-lag | replica 17분 뒤처짐 | db-down (폴백) |
| recommend-service | memory-leak | OOMKilled, `memory`/`heap` 단어 없음 | db-down (폴백) |

시스템 이름은 모두 `[a-z][a-z0-9-]+-service`다. 기존 이름의 부분문자열이 되는 이름(`catalog-service` 안의 `log-service` 같은 경우)은 ollama/openai 백엔드의 `s in query` 매칭을 오염시키므로 피했고, `check_data.py`가 검사한다. 회귀 쿼리 문서가 "목록에 없는 시스템"으로 쓰는 `billing-service`도 일부러 넣지 않았다.

## 2. selector_cases (40건)

`{"id","system","status","alerts","query","expected","category"}` 형식이다.

| category | 건수 | 설계 | 개선 전 규칙 정답 |
|---|---|---|---|
| keyword | 12 | alerts에 규칙 키워드가 있고 올바른 유형으로 매핑됨 | 12/12 |
| paraphrase (= 규칙 키워드 없음) | 18 | 규칙 키워드가 전혀 없고(상태는 degraded) 의미는 분명함, 정답은 db-down 아님 | 0/18 |
| trap | 10 | 규칙 키워드가 있지만 규칙이 엉뚱한 유형을 고름 (예: `Lock wait timeout` -> network-timeout) | 0/10 |

- 유형별 분포: 14개 유형 모두 2건 이상 (high-latency, network-timeout 2건 / 나머지 3건).
- query는 짧은 한국어 당직 메시지이고 유형 이름이나 직접 힌트(`인증서`, `DNS`, `복제`, `디스크` 등)를 쓰지 않는다. 유형별 금지어 목록으로 검사한다.
- **주의: 개선 전 30%(12/40)는 규칙의 실제 정확도가 아니다.** 규칙이 약한 구간(paraphrase, trap)을 70% 채우도록 일부러 설계한 분포의 결과이고(`check_data.py`가 범주별 규칙 정오를 단언한다), 신규 8개 유형은 규칙이 애초에 출력할 수 없다. After 수치는 "같은 40건에서 얼마나 올랐나"로만 해석해야 한다.
- 범주 이름 "paraphrase"는 **"규칙 키워드가 없다"는 뜻일 뿐**이다. runbook 문장을 바꿔 말했다는 뜻이 아니고, 실제로 여러 케이스가 정답 runbook bullet의 거의 번역에 가깝다(4절).
- s10(keyword, 정답 high-latency)은 라벨이 애매하다. 알림에 지연 신호가 없어(`db connection pool 97% in use`, `requests queued waiting for a connection`) thread-pool-exhaustion·db-lock으로도 읽힌다. 정답은 규칙(`pool` → high-latency)을 따랐다. 결과는 s10 포함/제외를 함께 본다(IMPROVEMENTS.md §6.1).

## 3. e2e_cases (12건)

`{"id","query","expected":{"<system>":"<incident_type>"}}` 형식이다. 정답은 질의 문구가 아니라 해당 시스템 알림이 실제로 가리키는 유형이다 (`check_data.py`의 `SYSTEM_TRUTH`).

- 유형이 서로 다른 다중 시스템 케이스 4건 (e03, e07, e10, e12), web-01 함께 언급 2건 (e04, e10, 정답에서는 제외), 기존 6개 시스템만 쓰는 케이스 4건 (e01~e04).
- 모든 질의에 `장애|진단|지연|급증|다운` 중 하나가 있고, 시스템은 이름 그대로 언급한다. fake LLM의 `route()`/`plan()`을 직접 돌려 12건 모두 incident로 라우팅되고 조사 대상이 `정답 시스템 + web-01`과 정확히 일치함을 확인했다.
- 일부 질의는 일부러 알림과 다른 어감이다 (예: e05 ledger-service는 "지연"이라고 하지만 정답은 db-lock, e08 shipping-service도 "지연"이지만 rate-limit).
- 개선 전 기준선: 규칙은 질의된 시스템의 알림을 합쳐 runbook을 **하나만** 고르므로, 정답 17쌍(시스템, 유형) 중 5쌍만 맞출 수 있다.
- monitoring에 db-down 시스템이 없어서 e2e 정답에 db-down은 없다 (selector 쪽에서 3건 다룬다).

## 4. anti-leakage 결과 — 무엇을 검사했고 무엇은 못 막았나

**검사한 것 (표층 복사):** 각 alert 문장과 query를 소문자화하고 마크다운 기호(`` ` ``, `*`)와 중복 공백을 지운 뒤, 14개 runbook 전문 각각과의 최장 공통 부분문자열(`difflib.SequenceMatcher`)을 구해 최댓값을 본다. 기준은 20자 미만.

| 대상 | 최대 공통 길이 | 내용 |
|---|---|---|
| selector 40건 (alerts + query) | 15자 | `metadata lock` (db-lock, MySQL 용어라 불가피) |
| e2e 12건 질의 | 8자 | `gateway` |
| monitoring.json 전체 alerts | 16자 | `CrashLoopBackOff` (config-error, 고유명사) |

처음 작성본에서 `connection refused`가 18자로 가장 길어 문장을 바꿔 다시 썼다. 같은 언어로 문장을 베낀 곳은 없다.

**못 막은 것 (언어를 건넌 유사성, 고유 토큰):** 이 검사는 문자 단위라 영어 알림과 한국어 bullet 사이의 번역 수준 대응을 보지 못한다.
- 정답 bullet과 거의 번역 관계인 알림이 있다. s09 `ALTER TABLE … waiting for metadata lock` ↔ db-lock "`Waiting for table metadata lock` 상태의 세션들이 ALTER TABLE …"(bge-m3 cosine 0.676, selector·monitoring 알림 108개와 증상 bullet 사이 최댓값), s14 `inode usage 100%` ↔ disk-full "`df -i`에서 inode 사용률이 100%", s27 `same URL works when called by IP address` ↔ dns-failure "같은 서버에 IP로 직접 접속하면 성공한다".
- BM25 토크나이저 기준으로 paraphrase 18건 중 15건, e2e 시스템 14개 중 13개의 알림에 **정답 runbook에만 나오는 토큰**이 하나 이상 있다(inode, servfail, getaddrinfo, oomkilled, jstack, tomcat, crashloopbackoff 같은 용어와 on·for 같은 일반 단어가 섞여 있다). 실제 장애 용어라 runbook에 들어가는 게 자연스럽지만, 그만큼 검색이 쉬운 데이터다.
- 모니터링 값을 그대로 옮긴 runbook 예시값(`queue 200/200`, `exit code 137`)은 지웠다(1절). 지우기 전에는 recommend-service 알림(`… OOMKilled, exit code 137`)과 memory-leak bullet의 cosine이 0.710으로 가장 높았고, 지운 뒤 0.636이다.

한 사람이 runbook과 케이스를 같이 썼기 때문에 개념 수준의 유사성은 남아 있다. 건당 2.5%p인 작은 표본이라 소수점 차이에는 의미를 두지 않는 것이 좋다.

## 5. 키워드 규칙 sanity

개선 전 원본 `agent/graph.py`의 `_INCIDENT_TYPE_RULES`(개선 후에는 `agent/rules.py`의 `INCIDENT_TYPE_RULES`로 옮겼다. 순서대로 `latency, pool, cpu, memory, heap, timeout, network, disk, down`, 첫 매칭이 승리, 없으면 db-down)를 `check_data.py`에 복사해 status+alerts에 적용한다.

- selector: keyword 12/12 정답, paraphrase 0/18, trap 0/10, 전체 12/40. 위 설계 의도대로 단언(assert)한다.
- monitoring: 기존 5개(web-01 제외)는 규칙이 모두 정답, 신규 9개는 모두 오답 (db-down 7, network-timeout 1, high-latency 1).
- 규칙 사본은 개선 전 원본(`all_total_vue_student/agent/graph.py`)의 `_INCIDENT_TYPE_RULES`와 자동으로 대조한다. 개선 후 `agent/`가 규칙을 옮기거나 바꿔도 "Before" 기준선은 원본 규칙으로 고정된다.
