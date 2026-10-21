"""모니터링 서브 에이전트 — 계획된 시스템들의 지표를 MCP Tool로 병렬 수집한다.

노드가 collect_metrics 하나뿐인 작은 LangGraph지만, 메인 그래프에서는 '모니터링 담당'
노드 하나로 보인다. 지표 이상 탐지 같은 단계를 붙여도 메인 그래프는 바뀌지 않는다.
"""
import asyncio
import time
from functools import partial

from langgraph.graph import END, StateGraph
from langsmith import traceable

from ..deps import SupervisorDeps
from ..retry import with_retry
from ..state import AgentState

TASK_DELAY_SEC = 0.35   # 태스크당 고정 지연 — 병렬 효과를 실측하기 위함 (개선 전 research와 동일)


@traceable(name="collect_metrics")
async def collect_metrics(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 15 가드 + Wave 10b 병렬 + Wave 9 재시도 — get_monitoring_status Tool 실호출."""
    tasks = state["tasks"]
    pending = [i for i, t in enumerate(tasks)
               if t["agent"] == "research" and t["kind"] == "monitoring"
               and t["status"] == "pending"]
    # Wave 15 — 객체가 아니라 '실행할 목록'을 검사한다
    if not pending:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending monitoring task 0개"}]}

    async def run_one(idx: int) -> tuple[int, dict, int]:
        await asyncio.sleep(TASK_DELAY_SEC)
        data, retries = await with_retry(
            deps.mcp.call_tool, "get_monitoring_status", {"system_name": tasks[idx]["target"]})
        return idx, data, retries

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in pending))
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    retry_total = state.get("retry_count", 0)
    for idx, data, retries in results:
        new_tasks[idx]["status"] = "done"
        collected.append({"source": "monitoring", "data": data})
        retry_total += retries
        events.append({"type": "TaskCompleted", "id": new_tasks[idx]["id"],
                       "source": "monitoring", "target": new_tasks[idx]["target"],
                       "retries": retries})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events}


def build_monitoring_agent(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    g.add_node("collect_metrics", partial(collect_metrics, deps=deps))
    g.set_entry_point("collect_metrics")
    g.add_edge("collect_metrics", END)
    return g.compile(name="monitoring_agent")
