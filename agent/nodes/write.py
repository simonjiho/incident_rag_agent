from langgraph.config import get_stream_writer
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState, add_cost


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 8/10a — 고품질 모델로 초안 작성 (1차는 의도적 저품질).

    토큰마다 attempt 번호를 같이 실어 보낸다 — validate가 반려해 재작성(attempt 증가)
    될 때 프론트가 새 말풍선으로 끊어서 보여줄 수 있게 하기 위함(Wave 8 재작성 루프를
    그대로 노출)."""
    attempt = state.get("validation_attempts", 0) + 1
    writer = get_stream_writer()
    draft, usage = await deps.quality_llm.write(
        state["query"], state.get("collected", []),
        attempt, state.get("validation_feedback"),
        on_token=lambda piece: writer(
            {"type": "AnswerToken", "attempt": attempt, "delta": piece}))
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "cost_usd": add_cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}
