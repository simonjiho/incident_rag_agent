from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    return {"final": state["draft"],
            "events": state["events"] + [{"type": "Finalize", "pipeline": "incident-v2"}]}
