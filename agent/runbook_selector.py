"""runbook 선택기 — 시스템 '하나'의 모니터링 결과를 보고 어떤 runbook(incident_type)을 쓸지 고른다.

개선 전에는 replan_check가 조사한 모든 시스템의 알림을 한 덩어리로 합쳐 키워드 규칙으로
runbook을 하나만 골랐다. 지금은 시스템마다 따로 고르고, 고르는 방법을 갈아 끼울 수 있다.
  - KeywordSelector : 원래 키워드 규칙(agent.rules.classify_incident_type) — Before 기준선
  - McpRagSelector  : MCP Tool search_runbook(임베딩+BM25) — 확신이 없으면 키워드 규칙으로,
                      임베딩 서버 장애면 함께 온 BM25 순위로 폴백
runbook_agent는 selector.select(query, monitoring)만 부르므로 어느 쪽이 꽂혀 있는지 모른다(DI).
"""
from dataclasses import dataclass, field, replace

from .rules import classify_incident_type

# 검색 텍스트에 사용자 질의를 붙일지 — eval/run_eval.py selector의 '검색 텍스트 비교' 표로 정했다.
# 질의를 빼는 편이 vector·bm25 top-1과 세 방식 모두의 top-3에서 나았고, 질의 하나가 여러
# 시스템을 함께 말하는 경우 다른 시스템 증상이 섞여 들어가므로 알림만 쓴다.
INCLUDE_QUERY = False
# RAG top-1의 vector_score(cosine)가 이보다 낮으면 키워드 규칙으로 폴백한다.
# selector_cases 앞 10건(dev split)만 보고 정했다: 정답·오답 점수가 겹쳐(오답 0.566 > 정답 0.543)
# 깔끔한 경계가 없어서, dev 정답 최저점(0.543)보다 조금 낮은 보수적 하한을 쓴다.
# 재현: .venv/bin/python eval/run_eval.py threshold — 근거는 doc/IMPROVEMENTS.md.
DEFAULT_MIN_SCORE = 0.52
RAG_MODES = ("hybrid", "vector", "bm25")


@dataclass
class Selection:
    system: str
    incident_type: str | None     # None = runbook 불필요(정상 시스템) 또는 선택 실패
    score: float | None           # RAG top-1 cosine — 임계값과 비교하는 값 (bm25·키워드 규칙은 None)
    method: str                   # keyword | hybrid | vector | bm25 | bm25-degraded | keyword-fallback
    fallback: bool = False        # 설정한 방식 대신 다른 방식(키워드 규칙·BM25만)으로 골랐으면 True
    candidates: list[dict] = field(default_factory=list)   # RAG 후보 top-k (유형·점수)
    reason: str = ""              # 폴백·실패 사유: low-score / degraded / no-hit / replan / error
    margin: float | None = None   # RAG 1위 cosine − 나머지 후보 중 최고 cosine (작을수록 근소)


NEAR_TIE = 0.01   # 1·2위 cosine 차가 이보다 작으면 화면에 '근소'로 표시한다 (선택은 바꾸지 않음)


def top_margin(hits: list[dict]) -> float | None:
    """1위 cosine이 나머지 후보 중 가장 높은 cosine보다 얼마나 앞서는가 — 확신도 표시용.
    hybrid는 RRF 순위로 1위를 정하므로 음수(다른 후보의 cosine이 더 높음)도 나올 수 있다."""
    top = hits[0].get("vector_score") if hits else None
    others = [h["vector_score"] for h in hits[1:] if h.get("vector_score") is not None]
    if top is None or not others:
        return None
    return round(top - max(others), 4)


def needs_runbook(monitoring: dict) -> bool:
    """runbook이 필요한 시스템인가 — 정상(알림 없음)이거나 모니터링에 없는(unknown) 시스템은 제외."""
    status = monitoring.get("status")
    if status == "unknown":
        return False
    return status != "healthy" or bool(monitoring.get("alerts"))


def build_search_text(query: str, monitoring: dict, include_query: bool = INCLUDE_QUERY) -> str:
    """검색 질의 = 시스템명·상태 + 알림 문장들 (+ 사용자 질의)."""
    lines = [f"{monitoring.get('system_name', '')} status: {monitoring.get('status', '')}"]
    lines += monitoring.get("alerts", [])
    if include_query and query:
        lines.append(query)
    return "\n".join(lines)


class KeywordSelector:
    """개선 전 키워드 규칙을 시스템 하나에만 적용한다 (Before 기준선이자 RAG의 폴백)."""
    name = "keyword"

    async def select(self, query: str, monitoring: dict) -> Selection:
        return self.pick(monitoring)

    def pick(self, monitoring: dict) -> Selection:
        system = monitoring.get("system_name", "")
        if not needs_runbook(monitoring):
            return Selection(system, None, None, self.name)
        incident_type = classify_incident_type([{"source": "monitoring", "data": monitoring}])
        return Selection(system, incident_type, None, self.name)


class McpRagSelector:
    """MCP Tool search_runbook으로 고른다.
    임베딩 장애면 BM25 순위 1위, 무매칭·낮은 점수면 키워드 규칙으로 폴백한다."""

    def __init__(self, mcp, mode: str = "hybrid", min_score: float = DEFAULT_MIN_SCORE,
                 top_k: int = 3):
        if mode not in RAG_MODES:
            raise ValueError(f"mode must be one of {RAG_MODES}, got {mode!r}")
        self.mcp, self.mode, self.min_score, self.top_k = mcp, mode, min_score, top_k
        self.name = mode
        self.keyword = KeywordSelector()
        self._server: str | None = None

    async def select(self, query: str, monitoring: dict) -> Selection:
        if not needs_runbook(monitoring):
            return Selection(monitoring.get("system_name", ""), None, None, self.name)
        if self._server is None:   # search_runbook을 가진 서버를 먼저 찾는다 (서버 이름 하드코딩 X)
            self._server = await self.mcp.find_tool("search_runbook")
        result = await self.mcp.call_tool(
            "search_runbook",
            {"query": build_search_text(query, monitoring), "top_k": self.top_k,
             "mode": self.mode},
            server=self._server)
        return self.decide(monitoring, result)

    def decide(self, monitoring: dict, result: dict) -> Selection:
        """검색 결과 → Selection. 평가 스크립트도 이 함수를 그대로 써서 같은 규칙으로 잰다."""
        hits = result.get("hits", [])
        candidates = [{k: h[k] for k in ("incident_type", "score", "vector_score", "bm25_score")}
                      for h in hits]
        if not hits:
            return self._fallback(monitoring, None, candidates, "no-hit")
        system, top = monitoring["system_name"], hits[0]
        if result.get("degraded"):   # 임베딩 장애 — 서버가 BM25만으로 낸 순위를 그대로 쓴다
            return Selection(system, top["incident_type"], None, "bm25-degraded", fallback=True,
                             candidates=candidates, reason="degraded")
        # 임계값은 cosine(vector_score)에만 건다 — RRF 점수는 순위만 반영해 절대값 의미가 없고,
        # BM25 점수는 질의 내 최댓값으로 나눠 1위가 늘 1.0이다. bm25 모드는 임계값·점수 없이 1위.
        score = top["vector_score"]
        if score is not None and score < self.min_score:
            return self._fallback(monitoring, score, candidates, "low-score")
        return Selection(system, top["incident_type"], None if score is None else round(score, 4),
                         self.name, candidates=candidates, margin=top_margin(hits))

    def _fallback(self, monitoring: dict, score, candidates: list[dict], reason: str) -> Selection:
        """키워드 규칙 결과에 RAG 후보와 폴백 사유를 붙인다 (score = 기각된 RAG 점수)."""
        return replace(self.keyword.pick(monitoring), method="keyword-fallback", fallback=True,
                       score=None if score is None else round(score, 4),
                       candidates=candidates, reason=reason)


def build_selector(mcp, name: str = "hybrid", min_score: float = DEFAULT_MIN_SCORE):
    """RUNBOOK_SELECTOR 값 → 선택기. keyword | hybrid | vector | rag(=vector) | bm25."""
    if name == "keyword":
        return KeywordSelector()
    mode = "vector" if name == "rag" else name   # search_runbook Tool의 별칭 규칙과 같다
    if mode not in RAG_MODES:
        raise ValueError(f"RUNBOOK_SELECTOR must be keyword|hybrid|vector|rag|bm25, got {name!r}")
    return McpRagSelector(mcp, mode, min_score)
