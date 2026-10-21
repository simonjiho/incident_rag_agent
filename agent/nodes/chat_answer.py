from langgraph.config import get_stream_writer
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState, add_cost


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": add_cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}
