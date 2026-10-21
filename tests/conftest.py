"""공용 테스트 설정 — 프로젝트 루트를 import 경로에 추가하고 FakeMCP 픽스처를 제공한다.

실제 LLM·MCP subprocess·LangSmith·Ollama 없이 결정론적으로 돌아야 하므로, 추적은 끄고
MCP는 메모리 안의 FakeMCP로 대체한다 (search_runbook 결과도 미리 정해 둔 값을 돌려준다).
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LANGCHAIN_TRACING_V2"] = "false"   # 테스트는 LangSmith에 기록하지 않는다
os.environ["LANGSMITH_TRACING"] = "false"

import pytest

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.runbook_selector import build_selector

# 모니터링 스텁 데이터 — data/monitoring.json과 별개로 테스트 안에 고정한다
# (시스템이 추가돼도 테스트 결과가 바뀌지 않게).
FAKE_MONITORING = {
    "auth-service": {"status": "degraded", "cpu_percent": 80.0, "memory_percent": 62.0,
                     "alerts": ["p95 latency 2.4s (threshold 1.0s)",
                                "connection pool 92% used"]},
    "web-01": {"status": "healthy", "cpu_percent": 35.0, "memory_percent": 48.0,
               "alerts": []},
    "payments-service": {"status": "degraded", "cpu_percent": 92.0,
                         "memory_percent": 56.0,
                         "alerts": ["CPU 91% (threshold 80%)"]},
    "ledger-service": {"status": "degraded", "cpu_percent": 22.0, "memory_percent": 52.0,
                       "alerts": ["Lock wait timeout exceeded on ledger_entries"]},
}


def search_result(*hits: tuple[str, float], degraded: bool = False, mode: str = "hybrid") -> dict:
    """search_runbook Tool 응답 모양을 만든다. hits = (incident_type, cosine) 목록.
    degraded면 임베딩 서버 장애로 BM25만 쓴 응답(vector_score=None)을 흉내 낸다."""
    return {"mode": "bm25" if degraded else mode, "degraded": degraded, "elapsed_ms": 1.0,
            "hits": [{"incident_type": t, "score": round(1 / (60 + i), 4),
                      "vector_score": None if degraded else v, "bm25_score": 1.0,
                      "snippet": ""} for i, (t, v) in enumerate(hits, start=1)]}


# 검색 텍스트에 시스템 이름이 들어 있으면 이 결과를 돌려준다 (없으면 무매칭).
# ledger-service는 키워드 규칙이면 'timeout' 때문에 network-timeout으로 틀리는 사례다.
FAKE_SEARCH = {
    "auth-service": search_result(("high-latency", 0.71), ("thread-pool-exhaustion", 0.6)),
    "payments-service": search_result(("cpu-spike", 0.68), ("high-latency", 0.55)),
    "ledger-service": search_result(("db-lock", 0.66), ("network-timeout", 0.58)),
}


class FakeMCP:
    """MCPClient와 같은 메서드 이름만 가진 결정론적 스텁 (subprocess 없음).

    timeout_first_tool_call=True면 첫 get_monitoring_status 한 번만 asyncio.TimeoutError를
    던진다(MCPClient의 flaky 1회와 같음, with_retry 복구 확인용). search_results의 값을
    바꾸면 search_runbook 응답을 조작할 수 있고, 값이 예외 객체면 그 예외를 던진다.
    read_failures[incident_type] = n 이면 그 runbook 조회가 n번 실패한 뒤 성공한다.
    호출 기록은 tool_calls·resource_reads에 쌓인다 (실패한 조회도 기록)."""

    def __init__(self, timeout_first_tool_call: bool = False):
        self._timeout_pending = timeout_first_tool_call
        self.search_results: dict[str, object] = dict(FAKE_SEARCH)
        self.read_failures: dict[str, int] = {}
        self.tool_calls: list[tuple[str, dict]] = []
        self.resource_reads: list[str] = []

    async def list_tools(self, server: str) -> list:
        return [SimpleNamespace(name="get_monitoring_status"),
                SimpleNamespace(name="search_runbook")]

    async def find_tool(self, tool_name: str) -> str:
        names = [t.name for t in await self.list_tools("ops")]
        if tool_name not in names:
            raise ValueError(f"MCP tool not found on any connected server: {tool_name}")
        return "ops"

    async def find_prompt(self, prompt_name: str) -> str:
        return "ops"

    async def get_prompt(self, name: str, arguments: dict, server: str = "ops") -> str:
        return f"[FAKE PROMPT] 시스템: {arguments['system_name']} / 증상: {arguments['symptom']}"

    async def find_resource_template(self, template_name: str) -> tuple[str, str]:
        return "ops", "runbook://incident-response/{incident_type}"

    async def read_resource(self, uri: str, server: str) -> str:
        self.resource_reads.append(uri)
        incident_type = uri.rsplit("/", 1)[-1]
        if self.read_failures.get(incident_type, 0) > 0:
            self.read_failures[incident_type] -= 1
            raise ConnectionError(f"fake read failure: {incident_type}")
        return (f"# {incident_type} 대응 매뉴얼\n\n1. 영향 범위를 확인한다.\n"
                f"2. {incident_type} 원인을 점검한다.\n3. 조치 후 정상화를 확인한다.\n")

    async def call_tool(self, name: str, arguments: dict, server: str = "ops") -> dict:
        if self._timeout_pending and name == "get_monitoring_status":
            self._timeout_pending = False
            raise asyncio.TimeoutError("fake transient timeout")
        self.tool_calls.append((name, arguments))
        if name == "search_runbook":
            return self._search(arguments["query"])
        system = arguments["system_name"]
        data = FAKE_MONITORING.get(system, {
            "status": "unknown", "cpu_percent": 0.0, "memory_percent": 0.0,
            "alerts": [f"System '{system}' not found in monitoring"]})
        return {"system_name": system, "last_checked": "2026-01-01T00:00:00", **data}

    def _search(self, query: str) -> dict:
        system = next((s for s in self.search_results if s in query), None)
        result = self.search_results[system] if system else search_result()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_mcp() -> FakeMCP:
    return FakeMCP()


@pytest.fixture
def flaky_mcp() -> FakeMCP:
    """첫 call_tool에서 TimeoutError를 한 번 던지는 FakeMCP."""
    return FakeMCP(timeout_first_tool_call=True)


def make_deps(mcp: FakeMCP, selector: str = "hybrid") -> SupervisorDeps:
    """기본은 서버와 같은 hybrid RAG 선택기 (FakeMCP의 search_runbook을 부른다)."""
    return SupervisorDeps(
        fast_llm=FastLLM("fast-model-stub", usd_per_1k_tokens=0.00015),
        quality_llm=QualityLLM("quality-model-stub", usd_per_1k_tokens=0.005),
        mcp=mcp, session_store=None, version="v2",
        runbook_selector=build_selector(mcp, selector, min_score=0.5))


@pytest.fixture
def deps(fake_mcp) -> SupervisorDeps:
    return make_deps(fake_mcp)


@pytest.fixture
def flaky_deps(flaky_mcp) -> SupervisorDeps:
    return make_deps(flaky_mcp)


@pytest.fixture
def keyword_deps(fake_mcp) -> SupervisorDeps:
    """개선 전과 같은 키워드 규칙 선택기 (RUNBOOK_SELECTOR=keyword)."""
    return make_deps(fake_mcp, selector="keyword")
