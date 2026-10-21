"""노드 단위 테스트 — 메인 그래프 노드와 서브 에이전트 노드를 그래프 없이 직접 호출한다.

get_stream_writer()를 쓰는 chat_answer·write 노드는 그래프 밖에서 부를 수 없으므로
test_graph_fake.py에서 그래프 전체로 확인한다."""
from conftest import FAKE_MONITORING

from agent.agents.monitoring_agent import collect_metrics
from agent.agents.runbook_agent import missing_systems, read_runbooks, select_runbooks
from agent.nodes import gate_node, replan_check_node, validate_node


def task(id: str, agent: str, kind: str, target: str = "", status: str = "pending") -> dict:
    return {"id": id, "agent": agent, "kind": kind, "target": target, "status": status}


# ---------- gate ----------

async def test_gate_removes_analysis_tasks_when_not_needed(deps):
    tasks = [task("t0", "research", "monitoring", "auth-service"),
             task("t1", "analysis", "pattern-analysis")]
    out = await gate_node({"tasks": tasks, "needs_analysis": False, "events": []}, deps)
    assert [t["id"] for t in out["tasks"]] == ["t0"]
    assert out["gate_removed"] == 1
    assert out["events"] == [{"type": "GateApplied", "removed_analysis_tasks": 1}]


async def test_gate_keeps_analysis_tasks_when_needed(deps):
    tasks = [task("t0", "research", "monitoring", "auth-service"),
             task("t1", "analysis", "pattern-analysis")]
    out = await gate_node({"tasks": tasks, "needs_analysis": True, "events": []}, deps)
    assert [t["id"] for t in out["tasks"]] == ["t0", "t1"]
    assert out["gate_removed"] == 0
    assert out["events"][-1]["removed_analysis_tasks"] == 0


# ---------- validate ----------

async def test_validate_rejects_short_draft_with_feedback(deps):
    out = await validate_node({"draft": "재시작하면 됩니다.", "events": []}, deps)
    assert out["validation_attempts"] == 1
    assert out["validation_feedback"] is not None
    assert "너무 짧습니다" in out["validation_feedback"]
    assert out["events"][-1] == {"type": "Validation", "attempt": 1, "verdict": "불합격"}


async def test_validate_rejects_long_draft_without_numbering(deps):
    out = await validate_node({"draft": "가" * 40, "events": []}, deps)
    assert out["validation_feedback"] is not None


async def test_validate_accepts_30_chars_with_numbering(deps):
    draft = "1) " + "가" * 27    # 정확히 30자
    assert len(draft) == 30
    out = await validate_node({"draft": draft, "events": []}, deps)
    assert out["validation_feedback"] is None
    assert out["events"][-1]["verdict"] == "합격"


async def test_validate_force_passes_on_third_attempt(deps):
    state = {"draft": "재시작하면 됩니다.", "validation_attempts": 2, "events": []}
    out = await validate_node(state, deps)
    assert out["validation_attempts"] == 3
    assert out["validation_feedback"] is None
    assert out["events"][-1]["verdict"].startswith("합격(최대 시도")


async def test_validate_second_attempt_still_rejects(deps):
    state = {"draft": "재시작하면 됩니다.", "validation_attempts": 1, "events": []}
    out = await validate_node(state, deps)
    assert out["validation_attempts"] == 2
    assert out["validation_feedback"] is not None


def mon(system: str) -> dict:
    """monitoring_agent가 collected에 쌓는 모양 그대로 만든다."""
    return {"source": "monitoring", "data": {"system_name": system, **FAKE_MONITORING[system]}}


def runbook(incident_type: str, *systems: str) -> dict:
    return {"source": "runbook", "incident_type": incident_type, "systems": list(systems),
            "data": f"# {incident_type}"}


def selection(system: str, incident_type: str | None, method: str = "hybrid") -> dict:
    return {"system": system, "incident_type": incident_type, "score": 0.7, "method": method,
            "fallback": False, "candidates": [], "reason": ""}


# ---------- monitoring_agent: collect_metrics ----------

async def test_collect_metrics_skipped_when_no_pending_task(deps, fake_mcp):
    tasks = [task("t0", "research", "monitoring", "auth-service", "done"),
             task("t1", "analysis", "pattern-analysis")]
    out = await collect_metrics({"tasks": tasks, "events": []}, deps)
    assert [e["type"] for e in out["events"]] == ["ResearchSkipped"]
    assert set(out) == {"events"}          # tasks·collected를 건드리지 않는다
    assert fake_mcp.tool_calls == []


async def test_collect_metrics_marks_monitoring_tasks_done(deps, fake_mcp):
    tasks = [task("task-0", "research", "monitoring", "auth-service"),
             task("task-1", "research", "monitoring", "web-01")]
    out = await collect_metrics({"tasks": tasks, "events": []}, deps)
    assert [t["status"] for t in out["tasks"]] == ["done", "done"]
    assert tasks[0]["status"] == "pending"      # 입력 상태는 변경하지 않는다
    assert [c["source"] for c in out["collected"]] == ["monitoring", "monitoring"]
    assert {c["data"]["system_name"] for c in out["collected"]} == {"auth-service", "web-01"}
    assert out["retry_count"] == 0
    assert out["parallel_elapsed"] >= 0.3       # 태스크당 고정 지연(0.35s)이 실제로 적용된다
    assert [e["type"] for e in out["events"]] == ["TaskCompleted", "TaskCompleted"]
    assert fake_mcp.resource_reads == []         # 모니터링 에이전트는 runbook을 읽지 않는다


async def test_collect_metrics_counts_retry_on_transient_timeout(flaky_deps):
    tasks = [task("task-0", "research", "monitoring", "auth-service"),
             task("task-1", "research", "monitoring", "web-01")]
    out = await collect_metrics({"tasks": tasks, "events": []}, flaky_deps)
    assert [t["status"] for t in out["tasks"]] == ["done", "done"]
    assert out["retry_count"] == 1
    assert sorted(e["retries"] for e in out["events"]) == [0, 1]


# ---------- runbook_agent: missing_systems / select_runbooks / read_runbooks ----------

def test_missing_systems_skips_healthy_and_covered():
    state = {"collected": [mon("auth-service"), mon("web-01"), mon("payments-service"),
                           runbook("cpu-spike", "payments-service")]}
    assert missing_systems(state) == ["auth-service"]   # web-01은 정상, payments는 이미 있음


async def test_select_runbooks_per_unhealthy_system(deps):
    state = {"query": "q", "events": [],
             "collected": [mon("auth-service"), mon("web-01"), mon("payments-service")]}
    out = await select_runbooks(state, deps)
    picked = {s["system"]: s["incident_type"] for s in out["runbook_selections"]}
    assert picked == {"auth-service": "high-latency", "payments-service": "cpu-spike"}
    assert [e["type"] for e in out["events"]] == ["RunbookSelected", "RunbookSelected"]
    assert out["events"][0] == {"type": "RunbookSelected", "system": "auth-service",
                                "incident_type": "high-latency", "score": 0.71,
                                "method": "hybrid", "fallback": False, "reason": ""}


async def test_select_runbooks_skips_when_all_healthy(deps, fake_mcp):
    out = await select_runbooks({"query": "q", "events": [], "collected": [mon("web-01")]}, deps)
    assert out["events"] == [{"type": "RunbookSkipped", "reason": "모든 시스템 정상",
                              "systems": []}]
    assert fake_mcp.tool_calls == []


async def test_select_runbooks_reports_unknown_systems_separately(deps, fake_mcp):
    """모니터링에 없는 시스템은 '정상'이 아니라 '모니터링에 없는 시스템'으로 따로 알린다."""
    unknown = {"source": "monitoring", "data": {"system_name": "foo-service", "status": "unknown",
                                                "alerts": ["System 'foo-service' not found"]}}
    out = await select_runbooks({"query": "q", "events": [],
                                 "collected": [unknown, mon("web-01")]}, deps)
    assert out["events"] == [{"type": "RunbookSkipped", "reason": "모니터링에 없는 시스템",
                              "systems": ["foo-service"]}]
    out = await select_runbooks({"query": "q", "events": [],
                                 "collected": [unknown, mon("auth-service")]}, deps)
    assert [e["type"] for e in out["events"]] == ["RunbookSkipped", "RunbookSelected"]


async def test_select_runbooks_records_selector_error(deps, fake_mcp):
    fake_mcp.search_results["payments-service"] = RuntimeError("tool crashed")
    state = {"query": "q", "events": [], "collected": [mon("payments-service")]}
    out = await select_runbooks(state, deps)
    sel = out["runbook_selections"][0]
    assert sel["incident_type"] is None
    assert sel["reason"].startswith("error")


async def test_select_runbooks_force_keyword_reselects_missing(deps, fake_mcp):
    state = {"query": "q", "events": [], "force_keyword": True,
             "collected": [mon("ledger-service")],
             "runbook_selections": [selection("ledger-service", None)]}
    out = await select_runbooks(state, deps)
    sel = out["runbook_selections"][-1]
    # 재계획 라운드는 RAG를 부르지 않고 키워드 규칙('timeout' → network-timeout)으로 고른다
    assert (sel["incident_type"], sel["method"], sel["fallback"], sel["reason"]) == (
        "network-timeout", "keyword", True, "replan")
    assert not any(name == "search_runbook" for name, _ in fake_mcp.tool_calls)


async def test_select_runbooks_force_keyword_keeps_selection_whose_read_failed(deps, fake_mcp):
    """선택은 됐는데 조회만 실패한 시스템은 재계획 라운드에서도 다시 고르지 않는다 (재조회 대상)."""
    state = {"query": "q", "events": [], "force_keyword": True,
             "collected": [mon("ledger-service")],
             "runbook_selections": [selection("ledger-service", "db-lock")]}
    out = await select_runbooks(state, deps)
    assert "runbook_selections" not in out and out["events"] == []
    assert fake_mcp.tool_calls == []


async def test_read_runbooks_groups_systems_by_type(deps, fake_mcp):
    state = {"tasks": [], "events": [],
             "collected": [mon("auth-service"), mon("payments-service")],
             "runbook_selections": [selection("auth-service", "high-latency"),
                                    selection("payments-service", "high-latency")]}
    out = await read_runbooks(state, deps)
    assert fake_mcp.resource_reads == ["runbook://incident-response/high-latency"]   # 1번만
    rb = [c for c in out["collected"] if c["source"] == "runbook"]
    assert [(c["incident_type"], c["systems"]) for c in rb] == [
        ("high-latency", ["auth-service", "payments-service"])]
    assert out["tasks"][-1]["status"] == "done"
    assert out["events"][-1] == {"type": "TaskCompleted", "id": "task-runbook-high-latency",
                                 "source": "runbook", "target": "high-latency",
                                 "systems": ["auth-service", "payments-service"], "retries": 0}


async def test_read_runbooks_reuses_already_read_type(deps, fake_mcp):
    state = {"tasks": [], "events": [],
             "collected": [mon("auth-service"), mon("payments-service"),
                           runbook("high-latency", "auth-service")],
             "runbook_selections": [selection("auth-service", "high-latency"),
                                    selection("payments-service", "high-latency")]}
    state["tasks"] = [{"id": "task-runbook-high-latency", "agent": "research", "kind": "runbook",
                       "target": "high-latency", "systems": ["auth-service"], "status": "done"}]
    out = await read_runbooks(state, deps)
    assert fake_mcp.resource_reads == []          # 다시 읽지 않고 적용 시스템만 추가
    rb = [c for c in out["collected"] if c["source"] == "runbook"]
    assert rb[0]["systems"] == ["auth-service", "payments-service"]
    assert out["tasks"][0]["systems"] == ["auth-service", "payments-service"]   # task도 같이
    assert state["tasks"][0]["systems"] == ["auth-service"]                     # 입력은 그대로
    assert out["events"] == [{"type": "RunbookShared", "target": "high-latency",
                              "systems": ["payments-service"]}]


async def test_read_runbooks_reports_failed_read_and_keeps_others(deps, fake_mcp):
    fake_mcp.read_failures["cpu-spike"] = 1
    state = {"tasks": [], "events": [],
             "collected": [mon("auth-service"), mon("payments-service")],
             "runbook_selections": [selection("auth-service", "high-latency"),
                                    selection("payments-service", "cpu-spike")]}
    out = await read_runbooks(state, deps)
    rb = [c["incident_type"] for c in out["collected"] if c["source"] == "runbook"]
    assert rb == ["high-latency"]                 # 실패한 유형만 빠지고 나머지는 읽는다
    failed = [e for e in out["events"] if e["type"] == "RunbookReadFailed"]
    assert failed == [{"type": "RunbookReadFailed", "target": "cpu-spike",
                       "systems": ["payments-service"],
                       "error": "fake read failure: cpu-spike"}]
    assert missing_systems(out) == ["payments-service"]          # replan_check가 다시 읽게 된다


# ---------- replan_check (Dynamic Planning) ----------

async def test_replan_check_sufficient_when_every_system_has_runbook(deps):
    state = {"events": [], "collected": [mon("auth-service"), mon("web-01"),
                                         runbook("high-latency", "auth-service")]}
    out = await replan_check_node(state, deps)
    assert out == {"events": [{"type": "ReplanCheck", "round": 0, "is_sufficient": True,
                               "target": []}]}


async def test_replan_check_requests_keyword_round_for_missing_system(deps):
    state = {"events": [], "collected": [mon("auth-service"), mon("payments-service"),
                                         runbook("high-latency", "auth-service")]}
    out = await replan_check_node(state, deps)
    assert out["replan_rounds"] == 1
    assert out["force_keyword"] is True
    assert out["events"][-1] == {"type": "ReplanCheck", "round": 0, "is_sufficient": False,
                                 "target": ["payments-service"]}


async def test_replan_check_gives_up_after_one_round(deps):
    state = {"events": [], "replan_rounds": 1, "collected": [mon("payments-service")]}
    out = await replan_check_node(state, deps)
    assert "replan_rounds" not in out
    assert out["events"][-1]["is_sufficient"] is True
    assert out["events"][-1]["target"] == ["payments-service"]   # 끝내 못 붙인 시스템


async def test_replan_check_all_healthy_needs_no_runbook(deps):
    out = await replan_check_node({"events": [], "collected": [mon("web-01")]}, deps)
    assert out["events"][-1]["is_sufficient"] is True
