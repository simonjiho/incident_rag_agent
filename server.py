"""all_total 통합 실습 — FastAPI 백엔드 (Wave 1·3·12·14 축 + 전체 조립).

- Wave 1  : lifespan에서 Agent 번들(MCP 연결·LLM·그래프)을 프로세스당 정확히 1회 생성,
            app.state로 모든 요청이 공유 (creation_count/instance_id로 증명)
- Wave 2  : mcp_server.py를 subprocess(stdio)로 기동해 연결 유지
- Wave 3  : GET /chat/stream — 그래프 실행 과정을 SSE 이벤트로 스트리밍
- Wave 12 : 대화 이력을 SQLite(session.db)에 저장 — 재시작해도 유지
- Wave 14 : SUPERVISOR_VERSION(v1/v2) 환경변수로 legacy/신형 파이프라인 즉시 전환
- runbook 선택기: RUNBOOK_SELECTOR(keyword/hybrid/vector/rag/bm25, 기본 hybrid)와
            RAG_MIN_SCORE로 lifespan에서 1회 만들어 SupervisorDeps에 주입
- LangSmith: 요청당 chat_request 루트 run 아래로 노드·LLM·MCP 호출이 계층 기록됨
- 사람 승인(HITL): hitl=true 요청은 위험 조치(재시작·롤백 등)가 든 최종안에서 그래프가 멈춘다.
            멈춘 상태는 InMemorySaver 체크포인터가 run_id(요청마다 새로 만드는 체크포인트
            thread)로 들고 있고, POST /chat/approve 또는 GET /chat/resume이 Command(resume=...)로
            이어서 실행한다. 대화 이력은 그대로 SQLite(thread_id)에 최종 답변만 남긴다.
"""
import json
import os
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()   # LANGCHAIN_TRACING_V2 / API_KEY / PROJECT — import보다 먼저

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.runbook_selector import DEFAULT_MIN_SCORE, build_selector
from agent.session_store import SqliteSessionStore
from agent.supervisor_v1 import run_v1

CREATION_COUNT = 0   # Wave 1 — 프로세스 내 Agent 번들 생성 횟수 (항상 1이어야 정상)


class ChatRequest(BaseModel):   # Wave 1 — Pydantic 요청 검증
    message: str
    thread_id: str = "default"
    hitl: bool = False            # 위험 조치가 있으면 사람 승인을 받고 내보낸다


class ApproveRequest(BaseModel):
    run_id: str
    approved: bool
    comment: str = ""


def _build_llms():
    """LLM_BACKEND=fake(기본, 결정론) | ollama(로컬 실모델) | openai | anthropic(Claude API)."""
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend == "anthropic":
        from agent.anthropic_llm import DEFAULT_MODEL, AnthropicFastLLM, AnthropicQualityLLM
        fast = AnthropicFastLLM(os.environ.get("ANTHROPIC_FAST_MODEL", DEFAULT_MODEL))
        quality = AnthropicQualityLLM(os.environ.get("ANTHROPIC_QUALITY_MODEL", DEFAULT_MODEL))
        return backend, fast, quality
    if backend == "ollama":
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b"),
                             usd_per_1k_tokens=0.00015)
        quality = OllamaQualityLLM(os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    if backend == "openai":
        from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
        fast = OpenAIFastLLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"),
                             usd_per_1k_tokens=0.00015)
        quality = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    fast = FastLLM("fast-model-stub", usd_per_1k_tokens=0.00015)
    quality = QualityLLM("quality-model-stub", usd_per_1k_tokens=0.005)
    return backend, fast, quality


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    CREATION_COUNT += 1
    mcp = MCPClient()
    await mcp.start()   # Wave 2 — MCP subprocess는 서버 수명 동안 딱 1개
    backend, fast_llm, quality_llm = _build_llms()
    app.state.llm_backend = backend
    version = os.environ.get("SUPERVISOR_VERSION", "v2")
    runbook_selector = None   # v1 legacy 경로는 runbook을 고르지 않는다 → /health에도 None
    if version == "v2":
        runbook_selector = build_selector(
            mcp, os.environ.get("RUNBOOK_SELECTOR", "hybrid"),
            float(os.environ.get("RAG_MIN_SCORE", DEFAULT_MIN_SCORE)))
    deps = SupervisorDeps(
        fast_llm=fast_llm,
        quality_llm=quality_llm,
        mcp=mcp,
        session_store=SqliteSessionStore(),
        version=version,
        runbook_selector=runbook_selector,
    )
    app.state.deps = deps
    # 체크포인터는 승인 대기(interrupt) 중인 실행을 이어 가는 데만 쓴다 — 끝난 실행은 지운다
    app.state.checkpointer = InMemorySaver()
    app.state.pending = {}   # run_id → 승인 대기 중인 요청 정보
    app.state.graph = (build_graph(deps, checkpointer=app.state.checkpointer)
                       if deps.version == "v2" else None)
    app.state.instance_id = id(deps)
    yield
    await mcp.stop()
    deps.session_store.close()
    try:   # 종료 전 LangSmith 배치 전송 플러시 — kill 시 run이 pending으로 남는 것 방지
        from langchain_core.tracers.langchain import wait_for_all_tracers
        wait_for_all_tracers()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "instance_id": app.state.instance_id,
            "creation_count": CREATION_COUNT,
            "supervisor_version": app.state.deps.version,
            "llm_backend": app.state.llm_backend,
            "fast_llm": app.state.deps.fast_llm.name,
            "quality_llm": app.state.deps.quality_llm.name,
            "runbook_selector": getattr(app.state.deps.runbook_selector, "name", None),
            "rag_min_score": getattr(app.state.deps.runbook_selector, "min_score", None)}


@app.get("/version")
async def version():
    return {"supervisor_version": app.state.deps.version}


def _metadata(result: dict, run_id: str | None) -> dict:
    return {
        "route": result.get("route"),
        "task_count": len(result.get("tasks", [])),
        "gate_removed": result.get("gate_removed", 0),
        "replan_rounds": result.get("replan_rounds", 0),
        "retry_count": result.get("retry_count", 0),
        "parallel_elapsed": result.get("parallel_elapsed"),
        "validation_attempts": result.get("validation_attempts", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "llm_backend": app.state.llm_backend,
        "supervisor_version": app.state.deps.version,
        "runbook_selector": getattr(app.state.deps.runbook_selector, "name", None),
        "runbook_selections": result.get("runbook_selections", []),
        "instance_id": app.state.instance_id,
        "langsmith_run_id": run_id,
        "hitl": bool(result.get("hitl")),
        "approval_rounds": result.get("approval_rounds", 0),
    }


def _new_run(thread_id: str) -> tuple[str, dict]:
    """요청마다 체크포인트 thread를 새로 만든다 — 같은 대화(thread_id)라도 이전 요청의
    그래프 상태가 다음 요청에 섞이지 않게. 대화 이력은 SQLite가 따로 들고 있다."""
    run_id = f"{thread_id}:{uuid.uuid4().hex[:8]}"
    return run_id, {"configurable": {"thread_id": run_id}}


def _interrupt_value(result: dict) -> dict | None:
    interrupts = result.get("__interrupt__") or ()
    return interrupts[0].value if interrupts else None


async def _finish(run_id: str, thread_id: str, message: str, final: str) -> None:
    deps = app.state.deps
    deps.session_store.append(thread_id, "user", message)
    deps.session_store.append(thread_id, "assistant", final)
    await app.state.checkpointer.adelete_thread(run_id)


def _approval_response(result: dict, approval: dict, run_id: str, rid: str | None) -> dict:
    return {"status": "approval_required", "run_id": run_id, "approval": approval,
            "reply": None, "events": result.get("events", []),
            "metadata": _metadata(result, rid)}


@traceable(name="chat_request")
async def _handle_chat(message: str, thread_id: str, hitl: bool = False,
                       run_id: str | None = None,
                       config: dict | None = None) -> tuple[dict, str | None]:
    deps = app.state.deps
    history = deps.session_store.load(thread_id)   # Wave 12
    if deps.version == "v2":
        result = await app.state.graph.ainvoke(
            {"query": message, "thread_id": thread_id, "history": history,
             "events": [], "cost_usd": 0.0, "hitl": hitl}, config)
    else:
        result = await run_v1(deps, message, thread_id, history)   # Wave 14
    if _interrupt_value(result) is None:
        if deps.version == "v2":
            await _finish(run_id, thread_id, message, result["final"])
        else:
            deps.session_store.append(thread_id, "user", message)
            deps.session_store.append(thread_id, "assistant", result["final"])
    rt = get_current_run_tree()
    return result, (str(rt.id) if rt else None)


@traceable(name="chat_approve")
async def _handle_approve(req: ApproveRequest, pending: dict) -> tuple[dict, str | None]:
    result = await app.state.graph.ainvoke(
        Command(resume={"approved": req.approved, "comment": req.comment}), pending["config"])
    if _interrupt_value(result) is None:
        await _finish(req.run_id, pending["thread_id"], pending["message"], result["final"])
    rt = get_current_run_tree()
    return result, (str(rt.id) if rt else None)


def _take_pending(run_id: str) -> dict:
    pending = app.state.pending.pop(run_id, None)
    if pending is None:
        raise HTTPException(404, f"승인 대기 중인 실행이 없습니다: {run_id}")
    return pending


@app.post("/chat")
async def chat(req: ChatRequest):
    run_id, config = _new_run(req.thread_id)
    result, rid = await _handle_chat(req.message, req.thread_id, req.hitl, run_id, config)
    approval = _interrupt_value(result)
    if approval is not None:
        app.state.pending[run_id] = {"thread_id": req.thread_id, "message": req.message,
                                     "config": config, "seen": len(result.get("events", []))}
        return _approval_response(result, approval, run_id, rid)
    return {"status": "done", "reply": result["final"], "events": result.get("events", []),
            "metadata": _metadata(result, rid)}


@app.post("/chat/approve")
async def chat_approve(req: ApproveRequest):
    """승인 대기 중인 실행(run_id)에 사람의 답을 넣어 이어서 실행한다 (JSON 응답)."""
    pending = _take_pending(req.run_id)
    result, rid = await _handle_approve(req, pending)
    approval = _interrupt_value(result)
    if approval is not None:   # 거부 → 다시 쓴 안에도 위험 조치가 남아 한 번 더 묻는다
        app.state.pending[req.run_id] = {**pending, "seen": len(result.get("events", []))}
        return _approval_response(result, approval, req.run_id, rid)
    return {"status": "done", "reply": result["final"], "events": result.get("events", []),
            "metadata": _metadata(result, rid)}


async def _stream_graph(graph_input, config: dict, run_id: str, thread_id: str,
                        message: str, seen: int = 0):
    """그래프 실행을 SSE 이벤트로 흘려보낸다 — 새 요청과 승인 후 재개가 같이 쓴다.

    stream_mode를 리스트로 주면 (mode, payload) 튜플이 나온다 — "updates"는 노드가 끝날
    때마다의 상태 델타, "custom"은 write_node/chat_answer_node가 get_stream_writer()로
    실행 중에 즉시 흘려보내는 토큰(AnswerToken) 이벤트다. subgraphs=True면 서브 에이전트
    (monitoring_agent/runbook_agent) 안쪽 노드가 끝날 때마다도 델타가 오고, 튜플 앞에
    namespace(어느 서브그래프인지)가 붙는다. approval 노드가 interrupt()로 멈추면
    updates에 '__interrupt__' 키가 온다 — 이때는 FinalAnswer 대신 ApprovalRequired를 보낸다.
    """
    final = ""
    last_prompt = None   # finalize까지 살아남은 최종 write/chat 호출의 실제 프롬프트
    approval = None
    async for namespace, mode, payload in app.state.graph.astream(
            graph_input, config, stream_mode=["updates", "custom"], subgraphs=True):
        if mode == "custom":
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            continue
        agent_name = namespace[0].split(":")[0] if namespace else None
        for node_name, delta in payload.items():
            if node_name == "__interrupt__":
                approval = delta[0].value if delta else None
                continue
            delta = delta or {}
            for ev in (delta.get("events") or [])[seen:]:
                ev = {**ev, "node": node_name}
                if agent_name:
                    ev["agent"] = agent_name
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if delta.get("events"):
                seen = len(delta["events"])
            if delta.get("final"):
                final = delta["final"]
            if delta.get("last_prompt"):
                last_prompt = delta["last_prompt"]
    if approval is not None:
        app.state.pending[run_id] = {"thread_id": thread_id, "message": message,
                                     "config": config, "seen": seen}
        ev = {**approval, "type": "ApprovalRequired", "run_id": run_id}
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    else:
        yield f"data: {json.dumps({'type': 'FinalAnswer', 'reply': final, 'prompt': last_prompt}, ensure_ascii=False)}\n\n"
        await _finish(run_id, thread_id, message, final)
    yield "data: [DONE]\n\n"


@app.get("/chat/stream")
async def chat_stream(message: str, thread_id: str = "default", hitl: bool = False):
    """Wave 3 — 그래프의 노드별 진행 상황을 SSE로 스트리밍.

    이벤트마다 'data: {json}\\n\\n' 형태로 내려보내고 마지막에 data: [DONE] 센티널을
    보낸다 — 프론트엔드는 \\n\\n 경계로 이벤트를 분리해 파싱해야 한다(Wave 3b).
    hitl=true면 위험 조치가 든 최종안에서 ApprovalRequired(run_id 포함)를 보내고 멈춘다.
    """
    deps = app.state.deps

    async def gen_v1():
        history = deps.session_store.load(thread_id)
        result = await run_v1(deps, message, thread_id, history)
        for ev in result["events"]:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'FinalAnswer', 'reply': result['final']}, ensure_ascii=False)}\n\n"
        deps.session_store.append(thread_id, "user", message)
        deps.session_store.append(thread_id, "assistant", result["final"])
        yield "data: [DONE]\n\n"

    if deps.version != "v2":
        return StreamingResponse(gen_v1(), media_type="text/event-stream")
    run_id, config = _new_run(thread_id)
    graph_input = {"query": message, "thread_id": thread_id,
                   "history": deps.session_store.load(thread_id),
                   "events": [], "cost_usd": 0.0, "hitl": hitl}
    return StreamingResponse(_stream_graph(graph_input, config, run_id, thread_id, message),
                             media_type="text/event-stream")


@app.get("/chat/resume")
async def chat_resume(run_id: str, approved: bool, comment: str = ""):
    """승인/거부 후 이어지는 실행을 SSE로 — 거부면 write가 다시 돌며 토큰이 흐른다."""
    pending = _take_pending(run_id)
    return StreamingResponse(
        _stream_graph(Command(resume={"approved": approved, "comment": comment}),
                      pending["config"], run_id, pending["thread_id"], pending["message"],
                      seen=pending["seen"]),
        media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
