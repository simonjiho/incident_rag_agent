"""agent/runbook_selector.py — 키워드/RAG 선택기와 MCPClient.find_tool 단위 테스트.

RAG 쪽은 FakeMCP가 search_runbook 응답을 미리 정해 둔 값으로 돌려준다 (Ollama 불필요)."""
from types import SimpleNamespace

import pytest
from conftest import search_result

from agent.mcp_client import MCPClient
from agent.runbook_selector import (KeywordSelector, McpRagSelector, build_search_text,
                                    build_selector, needs_runbook)

LEDGER = {"system_name": "ledger-service", "status": "degraded",
          "alerts": ["Lock wait timeout exceeded on ledger_entries"]}
HEALTHY = {"system_name": "web-01", "status": "healthy", "alerts": []}


def search_calls(mcp) -> list[dict]:
    return [args for name, args in mcp.tool_calls if name == "search_runbook"]


# ---------- 공통 규칙 ----------

def test_needs_runbook():
    assert needs_runbook(LEDGER)
    assert not needs_runbook(HEALTHY)
    assert needs_runbook({"status": "healthy", "alerts": ["disk 85%"]})   # 정상이어도 알림이 있으면
    assert not needs_runbook({"status": "unknown", "alerts": ["System 'x' not found"]})


def test_build_search_text_alerts_only_by_default():
    text = build_search_text("쓰기가 멈췄어요", LEDGER)
    assert text == ("ledger-service status: degraded\n"
                    "Lock wait timeout exceeded on ledger_entries")
    assert build_search_text("쓰기가 멈췄어요", LEDGER, include_query=True).endswith("쓰기가 멈췄어요")


# ---------- KeywordSelector ----------

async def test_keyword_selector_applies_original_rule_to_one_system():
    sel = await KeywordSelector().select("", LEDGER)
    assert (sel.system, sel.incident_type, sel.score, sel.method, sel.fallback) == (
        "ledger-service", "network-timeout", None, "keyword", False)   # 'timeout'에 낚인다


async def test_keyword_selector_healthy_is_none():
    assert (await KeywordSelector().select("", HEALTHY)).incident_type is None


def test_keyword_selector_falls_back_to_db_down():
    sel = KeywordSelector().pick({"system_name": "x", "status": "degraded", "alerts": ["odd"]})
    assert sel.incident_type == "db-down"


# ---------- McpRagSelector ----------

async def test_rag_confident_hit(fake_mcp):
    selector = McpRagSelector(fake_mcp, "hybrid", min_score=0.5)
    sel = await selector.select("쓰기가 멈췄어요", LEDGER)
    assert (sel.incident_type, sel.score, sel.method, sel.fallback, sel.reason) == (
        "db-lock", 0.66, "hybrid", False, "")
    assert [c["incident_type"] for c in sel.candidates] == ["db-lock", "network-timeout"]
    assert search_calls(fake_mcp) == [{"query": build_search_text("쓰기가 멈췄어요", LEDGER),
                                       "top_k": 3, "mode": "hybrid"}]


async def test_rag_low_score_falls_back_to_keyword(fake_mcp):
    fake_mcp.search_results["ledger-service"] = search_result(("rate-limit", 0.41))
    sel = await McpRagSelector(fake_mcp, "hybrid", min_score=0.5).select("", LEDGER)
    assert (sel.incident_type, sel.method, sel.fallback, sel.reason, sel.score) == (
        "network-timeout", "keyword-fallback", True, "low-score", 0.41)
    assert sel.candidates[0]["incident_type"] == "rate-limit"   # 기각된 RAG 후보도 남긴다


async def test_rag_degraded_uses_bm25_top1(fake_mcp):
    """임베딩 서버 장애(degraded)여도 BM25 순위가 있으면 그것을 쓴다 — 키워드 규칙(network-timeout)보다 낫다."""
    fake_mcp.search_results["ledger-service"] = search_result(
        ("db-lock", 0.0), ("rate-limit", 0.0), degraded=True)
    sel = await McpRagSelector(fake_mcp, "hybrid", min_score=0.5).select("", LEDGER)
    assert (sel.incident_type, sel.method, sel.fallback, sel.reason, sel.score) == (
        "db-lock", "bm25-degraded", True, "degraded", None)
    assert [c["incident_type"] for c in sel.candidates] == ["db-lock", "rate-limit"]


async def test_rag_degraded_without_hits_falls_back_to_keyword(fake_mcp):
    fake_mcp.search_results["ledger-service"] = search_result(degraded=True)
    sel = await McpRagSelector(fake_mcp, "hybrid", min_score=0.5).select("", LEDGER)
    assert (sel.incident_type, sel.method, sel.reason) == (
        "network-timeout", "keyword-fallback", "no-hit")


async def test_rag_no_hit_falls_back_to_keyword(fake_mcp):
    fake_mcp.search_results["ledger-service"] = search_result()
    sel = await McpRagSelector(fake_mcp, "hybrid", min_score=0.5).select("", LEDGER)
    assert (sel.method, sel.reason) == ("keyword-fallback", "no-hit")


async def test_rag_healthy_is_none_without_calling_mcp(fake_mcp):
    sel = await McpRagSelector(fake_mcp, "hybrid").select("", HEALTHY)
    assert sel.incident_type is None
    assert fake_mcp.tool_calls == []


async def test_rag_bm25_mode_has_no_threshold(fake_mcp):
    """bm25 모드는 cosine이 없으므로(vector_score=None) 임계값 없이 top-1을 쓰고 점수는 남기지 않는다
    (정규화 BM25 1위는 늘 1.0이라 확신도로 읽히면 안 된다)."""
    bm25_only = search_result(("db-lock", 0.0), mode="bm25")
    bm25_only["hits"][0].update(vector_score=None, score=1.0)
    fake_mcp.search_results["ledger-service"] = bm25_only
    sel = await McpRagSelector(fake_mcp, "bm25", min_score=0.9).select("", LEDGER)
    assert (sel.incident_type, sel.method, sel.fallback, sel.score) == (
        "db-lock", "bm25", False, None)


async def test_rag_finds_server_once(fake_mcp):
    calls = []
    original = fake_mcp.find_tool

    async def counting_find_tool(name):
        calls.append(name)
        return await original(name)

    fake_mcp.find_tool = counting_find_tool
    selector = McpRagSelector(fake_mcp, "hybrid")
    await selector.select("", LEDGER)
    await selector.select("", LEDGER)
    assert calls == ["search_runbook"]


# ---------- build_selector ----------

def test_build_selector_names(fake_mcp):
    assert isinstance(build_selector(fake_mcp, "keyword"), KeywordSelector)
    assert build_selector(fake_mcp, "hybrid", 0.4).min_score == 0.4
    assert build_selector(fake_mcp, "rag").mode == "vector"   # search_runbook과 같은 별칭
    assert build_selector(fake_mcp, "bm25").name == "bm25"
    with pytest.raises(ValueError):
        build_selector(fake_mcp, "llm")


# ---------- MCPClient.find_tool (subprocess 없이 세션만 흉내) ----------

class StubSession:
    def __init__(self, *tool_names: str):
        self.tool_names = tool_names

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in self.tool_names])


async def test_mcp_client_find_tool_searches_connected_servers():
    client = MCPClient(flaky_first_tool_call=False)
    client.sessions = {"docs": StubSession(), "ops": StubSession("get_monitoring_status",
                                                                 "search_runbook")}
    assert await client.find_tool("search_runbook") == "ops"
    with pytest.raises(ValueError):
        await client.find_tool("no_such_tool")


def test_top_margin_flags_near_tie():
    """e03 cache-service 재현 — RRF 1위 disk-full과 2위 memory-leak의 cosine 차가 0.0007."""
    from agent.runbook_selector import NEAR_TIE, top_margin
    hits = search_result(("disk-full", 0.6052), ("memory-leak", 0.6045), ("cpu-spike", 0.51))["hits"]
    assert top_margin(hits) == 0.0007 and top_margin(hits) < NEAR_TIE
    assert top_margin(search_result(("db-lock", 0.66), ("network-timeout", 0.58))["hits"]) == 0.08
    assert top_margin(search_result(("db-lock", 0.66))["hits"]) is None          # 후보 하나
    assert top_margin(search_result(("db-lock", 0.6), degraded=True)["hits"]) is None   # BM25만


async def test_rag_selection_records_margin(fake_mcp):
    sel = await McpRagSelector(fake_mcp, "hybrid", min_score=0.5).select("쓰기가 멈췄어요", LEDGER)
    assert sel.incident_type == "db-lock" and sel.margin == 0.08
