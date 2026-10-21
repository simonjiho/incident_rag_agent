"""그래프 전체가 공유하는 상태(AgentState)와 비용 누적 헬퍼."""
from typing import TypedDict


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    runbook_selections: list[dict]   # 시스템별 runbook 선택 기록 (Selection을 dict로)
    force_keyword: bool              # replan_check가 켜면 runbook_agent가 키워드 규칙으로 재선택
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_feedback: str
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]


def add_cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)
