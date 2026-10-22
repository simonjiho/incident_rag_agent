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

# 서비스를 멈추거나 데이터를 바꾸는 조치. 영어 동사는 단어 단위로만 잡는다 —
# 'pod restarted 9 times', 'OOMKilled' 같은 모니터링 관찰 문장이 조치로 잡히지 않게.
RISKY_PATTERN = re.compile(
    r"재시작|재기동|재부팅|롤백|삭제|강제\s*종료|페일오버|"
    r"\b(?:restart|reboot|roll\s?back|kill|delete|failover|truncate)\b|"
    r"pg_terminate_backend|drop\s+(?:table|database)|kubectl\s+(?:delete|drain|scale)",
    re.IGNORECASE)
_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+(?=\S)")
MAX_ACTIONS = 5
SNIPPET = 160      # 승인 카드·거부 피드백에 보여 줄 길이
LEAD = 60          # 위험 단어 앞에 남길 문맥


def find_risky_actions(text: str) -> list[str]:
    """최종안에서 위험 조치가 들어 있는 문장을 찾는다 (최대 5개).
    긴 문장은 위험 단어가 꼭 보이도록 그 단어 앞뒤로 잘라 보여 준다."""
    found = []
    for sentence in _SPLIT.split(text or ""):
        line = sentence.strip(" -*")
        m = RISKY_PATTERN.search(line)
        if not line or not m:
            continue
        start = 0 if len(line) <= SNIPPET else max(0, m.start() - LEAD)
        snippet = ("…" if start else "") + line[start:start + SNIPPET]
        if snippet not in found:
            found.append(snippet)
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
