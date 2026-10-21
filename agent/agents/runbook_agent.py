"""runbook 서브 에이전트 — 장애 시스템마다 runbook을 고르고(select) 읽어 온다(read).

select_runbooks → read_runbooks 두 노드짜리 LangGraph. 메인 그래프에는 노드 하나로 붙는다.
개선 전에는 모든 시스템의 알림을 합쳐 runbook을 하나만 골랐지만, 여기서는 시스템별로
고르고 같은 유형끼리 묶어 한 번씩만 읽는다.
"""
import asyncio
from dataclasses import asdict, replace
from functools import partial

from langgraph.graph import END, StateGraph
from langsmith import traceable

from ..deps import SupervisorDeps
from ..runbook_selector import KeywordSelector, Selection, needs_runbook
from ..state import AgentState
from .monitoring_agent import TASK_DELAY_SEC


def monitored(state: AgentState) -> list[dict]:
    return [c["data"] for c in state.get("collected", []) if c["source"] == "monitoring"]


def missing_systems(state: AgentState) -> list[str]:
    """runbook이 필요한데 아직 runbook을 하나도 못 읽은 시스템 (모니터링 순서 유지).
    replan_check도 이 함수로 '조사가 충분한가'를 판단한다."""
    covered = {s for c in state.get("collected", []) if c["source"] == "runbook"
               for s in c.get("systems", [])}
    systems = dict.fromkeys(d["system_name"] for d in monitored(state) if needs_runbook(d))
    return [s for s in systems if s not in covered]


@traceable(name="select_runbooks")
async def select_runbooks(state: AgentState, deps: SupervisorDeps) -> dict:
    """장애 시스템마다 runbook을 병렬로 고른다.

    대상은 '아직 유형을 못 고른' 시스템이다. 재계획 라운드(force_keyword)에서는 선택이 실패한
    시스템만 키워드 규칙으로 다시 고르고, 선택은 됐는데 조회만 실패한 시스템은 건드리지 않는다
    (read_runbooks가 같은 유형을 다시 읽는다)."""
    force = state.get("force_keyword", False)
    latest = {s["system"]: s["incident_type"] for s in state.get("runbook_selections", [])}
    missing = missing_systems(state)
    targets = [s for s in missing if not latest.get(s)]

    events = []
    unknown = [d["system_name"] for d in monitored(state) if d.get("status") == "unknown"]
    if unknown and not force:   # 모니터링에 없는 시스템은 근거가 없어 runbook을 고르지 않는다
        events.append({"type": "RunbookSkipped", "reason": "모니터링에 없는 시스템",
                       "systems": unknown})
    if not targets:
        if not missing and not unknown:
            events.append({"type": "RunbookSkipped", "reason": "모든 시스템 정상", "systems": []})
        return {"events": state["events"] + events}

    selector = KeywordSelector() if force else (deps.runbook_selector or KeywordSelector())
    by_system = {d["system_name"]: d for d in monitored(state)}

    async def select_one(system: str) -> Selection:
        try:
            selection = await selector.select(state["query"], by_system[system])
        except Exception as e:   # 선택기 장애로 진단이 멈추지 않게 — replan_check가 재계획한다
            return Selection(system, None, None, selector.name, reason=f"error: {e}")
        return replace(selection, fallback=True, reason="replan") if force else selection

    selections = await asyncio.gather(*(select_one(s) for s in targets))
    events += [{"type": "RunbookSelected", "system": s.system, "incident_type": s.incident_type,
                "score": s.score, "method": s.method, "fallback": s.fallback,
                "reason": s.reason} for s in selections]
    return {"runbook_selections": state.get("runbook_selections", []) +
            [asdict(s) for s in selections],
            "events": state["events"] + events}


@traceable(name="read_runbooks")
async def read_runbooks(state: AgentState, deps: SupervisorDeps) -> dict:
    """선택된 유형별로 runbook을 한 번씩만 읽는다 — 같은 유형인 시스템들은 묶는다.
    재계획 라운드에서는 지난번 조회가 실패한 유형도 여기서 다시 읽는다."""
    latest = {s["system"]: s["incident_type"] for s in state.get("runbook_selections", [])}
    wanted: dict[str, list[str]] = {}   # incident_type → 적용 시스템들
    for system in missing_systems(state):
        if latest.get(system):
            wanted.setdefault(latest[system], []).append(system)

    collected = [dict(c) for c in state.get("collected", [])]
    tasks = [dict(t) for t in state.get("tasks", [])]
    events = list(state["events"])
    for c in collected:   # 이미 읽은 유형이면 다시 읽지 않고 적용 시스템만 추가
        if c["source"] == "runbook" and c.get("incident_type") in wanted:
            added = wanted.pop(c["incident_type"])
            c["systems"] = c["systems"] + added
            for t in tasks:
                if t["id"] == f"task-runbook-{c['incident_type']}":
                    t["systems"] = c["systems"]
            events.append({"type": "RunbookShared", "target": c["incident_type"],
                           "systems": added})
    if not wanted:
        return {"tasks": tasks, "collected": collected, "events": events}

    # URI를 하드코딩하지 않고, runbook resource를 가진 서버를 먼저 찾는다
    server, uri_template = await deps.mcp.find_resource_template("get_incident_runbook")

    async def read_one(incident_type: str) -> str:
        await asyncio.sleep(TASK_DELAY_SEC)   # 개선 전 research와 같은 태스크당 지연 (공정 비교)
        return await deps.mcp.read_resource(
            uri_template.format(incident_type=incident_type), server=server)

    # return_exceptions=True — 한 유형을 못 읽어도 나머지는 살리고, 실패는 이벤트로 남긴다
    texts = await asyncio.gather(*(read_one(t) for t in wanted), return_exceptions=True)
    for (incident_type, systems), text in zip(wanted.items(), texts):
        if isinstance(text, BaseException):   # replan_check가 같은 유형을 한 번 더 읽게 한다
            events.append({"type": "RunbookReadFailed", "target": incident_type,
                           "systems": systems, "error": str(text)})
            continue
        task_id = f"task-runbook-{incident_type}"
        collected.append({"source": "runbook", "incident_type": incident_type,
                          "systems": systems, "data": text})
        tasks.append({"id": task_id, "agent": "research", "kind": "runbook",
                      "target": incident_type, "systems": systems, "status": "done"})
        events.append({"type": "TaskCompleted", "id": task_id, "source": "runbook",
                       "target": incident_type, "systems": systems, "retries": 0})
    return {"tasks": tasks, "collected": collected, "events": events}


def build_runbook_agent(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    g.add_node("select_runbooks", partial(select_runbooks, deps=deps))
    g.add_node("read_runbooks", partial(read_runbooks, deps=deps))
    g.set_entry_point("select_runbooks")
    g.add_edge("select_runbooks", "read_runbooks")
    g.add_edge("read_runbooks", END)
    return g.compile(name="runbook_agent")
