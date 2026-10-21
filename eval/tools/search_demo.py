"""MCP Tool search_runbook을 실제로 한 번 불러 보는 시연 스크립트.

에이전트와 같은 경로(MCPClient → find_tool → call_tool)로 부르고, 모니터링 알림 문장을
검색 텍스트로 넘겨 runbook 후보와 점수를 출력한다.
사용: .venv/bin/python eval/tools/search_demo.py [system-name ...]   (기본: ledger-service portal-service)
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.mcp_client import MCPClient                      # noqa: E402
from agent.runbook_selector import build_search_text        # noqa: E402


async def main(systems: list[str]) -> None:
    monitoring = json.loads((ROOT / "data" / "monitoring.json").read_text())
    mcp = MCPClient(flaky_first_tool_call=False)
    await mcp.start()
    try:
        server = await mcp.find_tool("search_runbook")
        print(f"search_runbook 제공 서버: {server}")
        for system in systems:
            mon = {"system_name": system, **monitoring[system]}
            text = build_search_text("", mon)
            result = await mcp.call_tool("search_runbook",
                                         {"query": text, "top_k": 3, "mode": "hybrid"}, server=server)
            print(f"\n[{system}] alerts: {mon['alerts']}")
            print(f"  mode={result['mode']} degraded={result['degraded']} elapsed_ms={result['elapsed_ms']}")
            for rank, hit in enumerate(result["hits"], 1):
                print(f"  {rank}. {hit['incident_type']:<24} cosine={hit['vector_score']:.3f}"
                      f"  bm25={hit['bm25_score']:.2f}  rrf={hit['score']:.4f}")
    finally:
        await mcp.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["ledger-service", "portal-service"]))
