"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | incident 파이프라인)
incident: plan(MCP Prompt 절차 반영) → gate(권위 게이트)
          → monitoring_agent(서브그래프: 지표 병렬 수집 + 재시도)
          → runbook_agent(서브그래프: 시스템별 runbook 선택 → 조회)
          → replan_check(runbook 빠진 시스템이 있으면 키워드 규칙으로 1회 재조사)
          → analysis(needs_analysis=True일 때만 실제 실행) → write → validate(반려 시 재작성)
          → finalize

이 파일에는 노드를 잇는 분기 함수와 build_graph만 남겼다. 나머지는 역할별로 나눠 두었다.
  - agent/state.py           : AgentState, add_cost
  - agent/rules.py           : 시스템명 추출·키워드 장애 유형 분류 규칙
  - agent/runbook_selector.py: runbook 선택기 (keyword / MCP RAG)
  - agent/agents/            : 조사 서브 에이전트 (컴파일된 LangGraph를 노드로 붙인다)
  - agent/nodes/             : 메인 그래프 노드 함수 (파일 하나에 노드 하나)

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
from functools import partial

from langgraph.graph import END, StateGraph

from .agents import build_monitoring_agent, build_runbook_agent
from .deps import SupervisorDeps
from .nodes import (
    analysis_node, chat_answer_node, finalize_node, gate_node, plan_node,
    replan_check_node, route_node, validate_node, write_node,
)
from .state import AgentState


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "plan"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("is_sufficient") else "runbook_agent"


def _after_validate(state: AgentState) -> str:
    return "finalize" if state.get("validation_feedback") is None else "write"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("monitoring_agent", build_monitoring_agent(deps))   # 서브그래프를 노드로
    g.add_node("runbook_agent", build_runbook_agent(deps))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan"})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "monitoring_agent")
    g.add_edge("monitoring_agent", "runbook_agent")
    g.add_edge("runbook_agent", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "runbook_agent": "runbook_agent"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
