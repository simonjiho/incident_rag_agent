"""fake LLM + FakeMCP로 v2 그래프 전체를 돌려 보는 결정론적 통합 테스트."""
from agent.graph import build_graph
from agent.supervisor_v1 import run_v1

INCIDENT_QUERY = "auth-service 장애 진단해줘"
MULTI_QUERY = "auth-service와 payments-service 둘 다 장애예요. 진단해줘"


def initial_state(query: str) -> dict:
    return {"query": query, "thread_id": "t", "history": [], "events": [],
            "cost_usd": 0.0}


def types(result: dict) -> list[str]:
    return [e["type"] for e in result["events"]]


def event(result: dict, type: str) -> dict:
    return next(e for e in result["events"] if e["type"] == type)


def events_of(result: dict, type: str) -> list[dict]:
    return [e for e in result["events"] if e["type"] == type]


async def test_incident_path(deps):
    result = await build_graph(deps).ainvoke(initial_state(INCIDENT_QUERY))

    assert "1)" in result["final"]
    assert result["route"] == "incident"
    assert result["validation_attempts"] == 2    # fake 1차 초안은 일부러 불량 → 1회 반려

    for t in ("RouteDecided", "PlanCreated", "GateApplied", "RunbookSelected",
              "ReplanCheck", "Validation", "Finalize"):
        assert t in types(result), t
    assert event(result, "GateApplied")["removed_analysis_tasks"] == 1
    assert event(result, "PlanCreated")["targets"] == ["auth-service", "web-01"]
    assert [e["verdict"] for e in result["events"] if e["type"] == "Validation"] == [
        "불합격", "합격"]
    assert result["events"][-1] == {"type": "Finalize", "pipeline": "incident-v2"}


async def test_incident_path_event_order(deps):
    result = await build_graph(deps).ainvoke(initial_state(INCIDENT_QUERY))
    seq = types(result)
    milestones = ["RouteDecided", "PlanCreated", "GateApplied", "TaskCompleted",
                  "RunbookSelected", "ReplanCheck", "DraftWritten", "Validation", "Finalize"]
    positions = [seq.index(m) for m in milestones]
    assert positions == sorted(positions)


async def test_incident_path_selects_runbook_per_system_without_replan(deps, fake_mcp):
    result = await build_graph(deps).ainvoke(initial_state(INCIDENT_QUERY))

    assert result.get("replan_rounds", 0) == 0          # runbook_agent가 1차에 바로 붙인다
    assert [c["is_sufficient"] for c in events_of(result, "ReplanCheck")] == [True]
    assert fake_mcp.resource_reads == ["runbook://incident-response/high-latency"]
    sel = result["runbook_selections"]
    assert [(s["system"], s["incident_type"], s["method"]) for s in sel] == [
        ("auth-service", "high-latency", "hybrid")]     # web-01은 정상이라 선택 대상 아님
    runbooks = [c for c in result["collected"] if c["source"] == "runbook"]
    assert [(c["incident_type"], c["systems"]) for c in runbooks] == [
        ("high-latency", ["auth-service"])]
    assert result["gate_removed"] == 1
    assert all(t["agent"] != "analysis" for t in result["tasks"])
    assert result["cost_usd"] > 0


async def test_multi_system_reads_two_different_runbooks(deps, fake_mcp):
    result = await build_graph(deps).ainvoke(initial_state(MULTI_QUERY))

    picked = {s["system"]: s["incident_type"] for s in result["runbook_selections"]}
    assert picked == {"auth-service": "high-latency", "payments-service": "cpu-spike"}
    assert sorted(fake_mcp.resource_reads) == [
        "runbook://incident-response/cpu-spike", "runbook://incident-response/high-latency"]
    read = {e["target"]: e["systems"] for e in events_of(result, "TaskCompleted")
            if e["source"] == "runbook"}
    assert read == {"high-latency": ["auth-service"], "cpu-spike": ["payments-service"]}
    # 최종 답변이 두 runbook을 모두, 어느 시스템용인지와 함께 담는다
    assert "[auth-service] 'high-latency 대응 매뉴얼'" in result["final"]
    assert "[payments-service] 'cpu-spike 대응 매뉴얼'" in result["final"]


async def test_rag_fixes_keyword_trap(deps, keyword_deps):
    """ledger-service 알림 'Lock wait timeout' — 키워드 규칙은 timeout에 낚인다."""
    query = "ledger-service 쓰기가 지연돼요. 장애 진단해줘"
    rag = await build_graph(deps).ainvoke(initial_state(query))
    keyword = await build_graph(keyword_deps).ainvoke(initial_state(query))
    assert rag["runbook_selections"][0]["incident_type"] == "db-lock"
    assert keyword["runbook_selections"][0]["incident_type"] == "network-timeout"
    assert keyword["runbook_selections"][0]["method"] == "keyword"


async def test_replan_falls_back_to_keyword_when_selector_fails(deps, fake_mcp):
    fake_mcp.search_results["auth-service"] = RuntimeError("search_runbook crashed")
    result = await build_graph(deps).ainvoke(initial_state(INCIDENT_QUERY))

    checks = events_of(result, "ReplanCheck")
    assert [(c["is_sufficient"], c["target"]) for c in checks] == [
        (False, ["auth-service"]), (True, [])]
    assert result["replan_rounds"] == 1
    first, second = events_of(result, "RunbookSelected")
    assert first["incident_type"] is None and first["reason"].startswith("error")
    assert (second["incident_type"], second["method"], second["reason"]) == (
        "high-latency", "keyword", "replan")
    assert fake_mcp.resource_reads == ["runbook://incident-response/high-latency"]
    assert "1)" in result["final"]


async def test_failed_read_is_retried_not_reselected(deps, fake_mcp):
    """조회가 한 번 실패해도 재계획 라운드는 RAG가 고른 db-lock을 다시 읽는다.
    키워드 규칙으로 다시 고르면 'timeout'에 낚여 network-timeout이 된다."""
    fake_mcp.read_failures["db-lock"] = 1
    result = await build_graph(deps).ainvoke(initial_state(
        "ledger-service와 auth-service 장애 진단해줘"))

    assert [(c["is_sufficient"], c["target"]) for c in events_of(result, "ReplanCheck")] == [
        (False, ["ledger-service"]), (True, [])]
    assert [e["target"] for e in events_of(result, "RunbookReadFailed")] == ["db-lock"]
    assert [(e["system"], e["incident_type"]) for e in events_of(result, "RunbookSelected")] == [
        ("ledger-service", "db-lock"), ("auth-service", "high-latency")]   # 다시 고르지 않는다
    assert fake_mcp.resource_reads.count("runbook://incident-response/db-lock") == 2
    assert "runbook://incident-response/network-timeout" not in fake_mcp.resource_reads
    assert "[ledger-service] 'db-lock 대응 매뉴얼'" in result["final"]


async def test_all_healthy_needs_no_runbook(deps, fake_mcp):
    result = await build_graph(deps).ainvoke(initial_state("web-01 장애 진단해줘"))
    assert event(result, "RunbookSkipped")["reason"] == "모든 시스템 정상"
    assert [c["is_sufficient"] for c in events_of(result, "ReplanCheck")] == [True]
    assert fake_mcp.resource_reads == []
    assert "2) 표준 절차: 적용할 runbook 없음" in result["final"]   # 1) 다음이 바로 3)이 아니다


async def test_unknown_system_is_not_reported_as_healthy(deps, fake_mcp):
    result = await build_graph(deps).ainvoke(initial_state("foo-service 장애 진단해줘"))
    skipped = events_of(result, "RunbookSkipped")
    assert skipped == [{"type": "RunbookSkipped", "reason": "모니터링에 없는 시스템",
                        "systems": ["foo-service"]}]
    assert fake_mcp.resource_reads == []          # 개선 전처럼 근거 없이 db-down을 읽지 않는다


async def test_incident_path_recovers_from_transient_timeout(flaky_deps):
    result = await build_graph(flaky_deps).ainvoke(initial_state(INCIDENT_QUERY))
    assert result["retry_count"] == 1
    assert "1)" in result["final"]


async def test_chat_path(deps, fake_mcp):
    result = await build_graph(deps).ainvoke(initial_state("안녕하세요"))

    assert result["route"] == "chat"
    assert result["final"]
    assert types(result) == ["RouteDecided", "Finalize"]
    assert result["events"][-1]["pipeline"] == "chat-direct"
    assert fake_mcp.tool_calls == []                    # 무거운 파이프라인을 타지 않는다


async def test_chat_path_streams_answer_tokens(deps):
    """get_stream_writer()가 custom 스트림으로 토큰을 내보내는지 — server.py /chat/stream 경로."""
    graph = build_graph(deps)
    chunks = [(mode, payload) async for mode, payload in graph.astream(
        initial_state("안녕하세요"), stream_mode=["updates", "custom"])]
    tokens = [p for mode, p in chunks if mode == "custom"]
    assert tokens and all(t["type"] == "AnswerToken" for t in tokens)
    final = next(p["chat_answer"]["final"] for mode, p in chunks
                 if mode == "updates" and "chat_answer" in p)
    assert "".join(t["delta"] for t in tokens) == final


async def test_incident_path_streams_answer_tokens_per_attempt(deps):
    graph = build_graph(deps)
    tokens = [p async for mode, p in graph.astream(
        initial_state(INCIDENT_QUERY), stream_mode=["custom"]) if p["type"] == "AnswerToken"]
    assert sorted({t["attempt"] for t in tokens}) == [1, 2]    # 재작성마다 attempt 증가


async def test_stream_with_subgraphs_reports_sub_agent_nodes(deps):
    """server.py /chat/stream은 subgraphs=True로 서브 에이전트 안쪽 노드 진행도 내보낸다."""
    graph = build_graph(deps)
    inner = [(ns[0].split(":")[0], node)
             async for ns, mode, payload in graph.astream(
                 initial_state(INCIDENT_QUERY), stream_mode=["updates"], subgraphs=True)
             if ns for node in payload]
    assert inner == [("monitoring_agent", "collect_metrics"),
                     ("runbook_agent", "select_runbooks"),
                     ("runbook_agent", "read_runbooks")]


async def test_v1_legacy_path_still_works(deps, fake_mcp):
    result = await run_v1(deps, INCIDENT_QUERY, "t", [])
    assert result["route"] == "legacy"
    assert "auth-service" in result["final"]
    assert result["events"][-1] == {"type": "Finalize", "pipeline": "legacy-v1"}
    assert fake_mcp.tool_calls == [("get_monitoring_status", {"system_name": "auth-service"})]
