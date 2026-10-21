from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}
