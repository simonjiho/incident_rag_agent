"""사람 승인(Human-in-the-loop) — 위험한 조치가 든 최종안은 사람이 승인해야 내보낸다.

validate를 통과한 최종안에서 재시작·롤백·삭제 같은 조치를 코드로 찾는다. LLM에게 위험한지
묻지 않는 이유는 gate(권위 게이트)와 같다 — 검증 가능한 판단은 코드가 내린다.
찾으면 interrupt()로 그래프를 멈추고, 서버가 사람의 답을 Command(resume=...)로 넣어 주면
이 노드가 처음부터 다시 실행되며 interrupt()가 그 답을 돌려준다.
  - 승인: 그대로 finalize
  - 거부: 그 조치를 빼라는 피드백으로 write를 한 번 다시 돌린다
          (다시 쓴 안도 거부하면 '승인되지 않아 보류' 문구를 붙여 finalize)
요청 상태에 hitl=True일 때만 동작한다 — 웹 화면은 켜고, API·평가 스크립트는 기본으로 끈다.
"""
import re

from langgraph.types import interrupt
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState

# (찾을 말, 사람에게 보여 줄 이름) — 서비스를 멈추거나 데이터를 바꾸는 조치
RISKY_ACTIONS = [
    ("재시작", "재시작"), ("재기동", "재기동"), ("restart", "restart"),
    ("롤백", "롤백"), ("rollback", "rollback"),
    ("삭제", "삭제"), ("kill", "kill"), ("pg_terminate_backend", "세션 강제 종료"),
    ("페일오버", "페일오버"), ("failover", "failover"), ("drop table", "DROP TABLE"),
]
_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+(?=\S)")
MAX_ACTIONS = 5


def find_risky_actions(text: str) -> list[str]:
    """최종안에서 위험 조치가 들어 있는 문장을 찾는다 (최대 5개, 원문 그대로)."""
    found = []
    for sentence in _SPLIT.split(text or ""):
        line = sentence.strip(" -*")
        lower = line.lower()
        if line and any(word in lower for word, _ in RISKY_ACTIONS) and line not in found:
            found.append(line[:160])
        if len(found) >= MAX_ACTIONS:
            break
    return found


@traceable(name="approval_node")
async def approval_node(state: AgentState, deps: SupervisorDeps) -> dict:
    if not state.get("hitl"):
        return {}
    actions = find_risky_actions(state.get("draft", ""))
    if not actions:
        return {"events": state["events"] +
                [{"type": "ApprovalSkipped", "reason": "위험 조치 없음"}]}

    rounds = state.get("approval_rounds", 0)
    decision = interrupt({"type": "ApprovalRequired", "actions": actions,
                          "draft": state.get("draft", ""), "round": rounds}) or {}
    approved = bool(decision.get("approved"))
    comment = str(decision.get("comment", "")).strip()
    event = {"type": "ApprovalDecision", "approved": approved, "actions": actions,
             "comment": comment, "rewrite": False}

    if approved:
        return {"events": state["events"] + [event]}
    if rounds >= 1:   # 다시 쓴 안도 거부 — 더 돌지 않고 보류 표시를 붙여 내보낸다
        note = "\n\n⚠️ 담당자가 승인하지 않아 보류한 조치: " + " / ".join(actions)
        return {"draft": state.get("draft", "") + note, "events": state["events"] + [event]}
    feedback = ("다음 조치는 담당자가 승인하지 않았다: " + "; ".join(actions)
                + (f" (사유: {comment})" if comment else "")
                + ". 이 조치를 빼고, 승인 없이 할 수 있는 확인·완화 절차만 1) 2) 3) 번호로 다시 작성하라.")
    return {"approval_rounds": rounds + 1, "validation_feedback": feedback,
            "events": state["events"] + [{**event, "rewrite": True}]}
