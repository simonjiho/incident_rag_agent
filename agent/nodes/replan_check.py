from langsmith import traceable

from ..agents.runbook_agent import missing_systems
from ..deps import SupervisorDeps
from ..state import AgentState


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 7 Dynamic Planning — 장애 시스템마다 runbook이 붙었는지 코드로 검사한다.

    runbook을 못 받은 시스템이 있으면 1회에 한해 force_keyword를 켜고 runbook_agent를 다시
    돌린다 — 선택기 오류로 유형이 없는 시스템은 키워드 규칙으로 다시 고르고, 조회만 실패한
    시스템은 이미 고른 유형을 다시 읽는다. 모든 시스템이 정상이면 runbook 없이도 충분하다."""
    missing = missing_systems(state)
    rounds = state.get("replan_rounds", 0)
    sufficient = not missing or rounds >= 1
    verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": sufficient,
               "target": missing}
    update = {"events": state["events"] + [verdict]}
    if not sufficient:
        update.update(replan_rounds=rounds + 1, force_keyword=True)
    return update
