"""사람 승인(HITL) — 위험 조치가 든 최종안에서 그래프가 멈추고, 승인/거부로 이어진다.

LangGraph interrupt()는 체크포인터가 있어야 멈춘 상태를 들고 있을 수 있다. 서버와 같이
InMemorySaver를 붙이고, 요청마다 다른 thread_id(config)로 돌린다.
"""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.fake_llm import QualityLLM
from agent.graph import build_graph
from agent.nodes.approval import find_risky_actions

QUERY = "auth-service 장애 진단해줘"
RISKY = ("1) auth-service CPU 80%, 알림 2건을 확인했다.\n"
         "2) 커넥션 풀이 92%라 auth-service 파드를 재시작한다.\n"
         "3) 배포 직후라면 이전 버전으로 롤백한다.")
SAFE = ("1) auth-service CPU 80%, 알림 2건을 확인했다.\n"
        "2) 커넥션 풀 사용률과 느린 쿼리를 조회해 원인을 좁힌다.\n"
        "3) 담당 팀에 공유하고 지표를 계속 지켜본다.")


class StubWriter(QualityLLM):
    """거부 피드백을 받으면 안전한 안을 쓴다 (always_risky면 계속 위험 조치를 쓴다)."""

    def __init__(self, always_risky: bool = False):
        super().__init__("quality-stub", usd_per_1k_tokens=0.005)
        self.always_risky = always_risky
        self.feedbacks: list[str | None] = []

    async def write(self, query, collected, attempt, feedback, on_token=None):
        self.feedbacks.append(feedback)
        rejected = feedback is not None and "승인하지 않았다" in feedback
        draft = SAFE if rejected and not self.always_risky else RISKY
        return draft, {"model": self.name, "tokens": 10, "usd": 0.0}


def initial_state(hitl: bool) -> dict:
    return {"query": QUERY, "thread_id": "t", "history": [], "events": [],
            "cost_usd": 0.0, "hitl": hitl}


def graph_with(deps, writer):
    deps.quality_llm = writer
    return build_graph(deps, checkpointer=InMemorySaver())


def config(name: str) -> dict:
    return {"configurable": {"thread_id": name}}


def types(result: dict) -> list[str]:
    return [e["type"] for e in result["events"]]


def test_find_risky_actions():
    found = find_risky_actions(RISKY)
    assert len(found) == 2
    assert "재시작" in found[0] and "롤백" in found[1]
    assert find_risky_actions(SAFE) == []
    assert find_risky_actions("kubectl rollout undo 대신 rollback 스크립트") != []
    assert len(find_risky_actions("\n".join(f"{i}) 서비스 재시작" for i in range(9)))) == 5


def test_observation_sentences_are_not_actions():
    """모니터링 알림 인용(restarted, OOMKilled)은 조치가 아니다 — 단어 단위로만 잡는다."""
    assert find_risky_actions("알림: pod restarted 9 times in 3h: reason OOMKilled, exit code 137") == []


def test_more_risky_actions_are_caught():
    text = ("1) kubectl delete pod auth-7f9 로 파드를 지운다.\n"
            "2) 노드를 재부팅한다.\n3) DROP DATABASE tmp_ledger 를 실행한다.\n"
            "4) 블로커는 `KILL` 또는 `pg_terminate_backend`로 종료한다.")
    assert len(find_risky_actions(text)) == 4


def test_long_sentence_snippet_keeps_the_risky_word():
    long = "진단 결과: " + "가" * 190 + " 즉시 재시작해 서비스 중단을 막는다."
    found = find_risky_actions(long)
    assert len(found) == 1 and "재시작" in found[0] and found[0].startswith("…")


async def test_hitl_off_runs_straight_through(deps):
    graph = graph_with(deps, StubWriter())
    result = await graph.ainvoke(initial_state(hitl=False), config("off"))
    assert "__interrupt__" not in result
    assert result["final"] == RISKY
    assert not any(t.startswith("Approval") for t in types(result))


class SafeWriter(StubWriter):
    async def write(self, query, collected, attempt, feedback, on_token=None):
        return SAFE, {"model": self.name, "tokens": 10, "usd": 0.0}


async def test_safe_draft_skips_approval(deps):
    graph = graph_with(deps, SafeWriter())
    result = await graph.ainvoke(initial_state(hitl=True), config("safe"))
    assert "__interrupt__" not in result
    assert "ApprovalSkipped" in types(result)
    assert result["final"] == SAFE


async def test_approve_resumes_to_final(deps):
    graph = graph_with(deps, StubWriter())
    cfg = config("approve")
    paused = await graph.ainvoke(initial_state(hitl=True), cfg)
    request = paused["__interrupt__"][0].value
    assert request["type"] == "ApprovalRequired" and request["round"] == 0
    assert len(request["actions"]) == 2

    result = await graph.ainvoke(Command(resume={"approved": True}), cfg)
    assert "__interrupt__" not in result
    assert result["final"] == RISKY
    decision = [e for e in result["events"] if e["type"] == "ApprovalDecision"]
    assert decision and decision[0]["approved"] is True
    assert result["events"][-1]["type"] == "Finalize"


async def test_reject_rewrites_without_the_action(deps):
    writer = StubWriter()
    graph = graph_with(deps, writer)
    cfg = config("reject")
    await graph.ainvoke(initial_state(hitl=True), cfg)
    result = await graph.ainvoke(Command(resume={"approved": False, "comment": "점검 시간 아님"}), cfg)

    assert "__interrupt__" not in result
    assert result["final"] == SAFE
    assert result["approval_rounds"] == 1
    assert "점검 시간 아님" in writer.feedbacks[-1] and "재시작" in writer.feedbacks[-1]
    seq = types(result)
    assert seq.index("ApprovalDecision") < seq.index("ApprovalSkipped") < seq.index("Finalize")


async def test_second_rejection_holds_the_action(deps):
    graph = graph_with(deps, StubWriter(always_risky=True))
    cfg = config("reject-twice")
    await graph.ainvoke(initial_state(hitl=True), cfg)
    again = await graph.ainvoke(Command(resume={"approved": False}), cfg)
    assert again["__interrupt__"][0].value["round"] == 1   # 다시 쓴 안도 위험 → 한 번 더 묻는다

    result = await graph.ainvoke(Command(resume={"approved": False}), cfg)
    assert "__interrupt__" not in result
    assert result["final"].startswith(RISKY)
    assert "승인하지 않아 보류한 조치" in result["final"]


@pytest.mark.parametrize("approved", [True, False])
async def test_runs_do_not_share_state(deps, approved):
    """요청마다 thread_id가 다르면 이전 요청의 승인 상태가 섞이지 않는다 (서버의 run_id 방식)."""
    graph = graph_with(deps, StubWriter())
    await graph.ainvoke(initial_state(hitl=True), config("a"))
    other = await graph.ainvoke(initial_state(hitl=True), config("b"))
    assert other["__interrupt__"][0].value["round"] == 0
    done = await graph.ainvoke(Command(resume={"approved": approved}), config("a"))
    assert "__interrupt__" not in done
