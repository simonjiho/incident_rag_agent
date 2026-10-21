"""all_total 통합 실습 — MCP Server (Wave 2 축).

Tool / Resource / Prompt 3대 capability를 모두 노출하는 실제 fastmcp 서버.
FastAPI 백엔드(server.py)가 lifespan에서 이 파일을 subprocess(stdio)로 1회 기동해
프로세스가 살아 있는 동안 재사용한다 (Wave 1 싱글턴 + Wave 2 표준화의 결합).

- Tool    get_monitoring_status : 호출마다 값이 달라지는 동적 조회 → Tool이 맞다
- Tool    search_runbook : 증상 문장 → 관련 runbook 순위 (RAG: 임베딩+BM25 하이브리드).
          질의마다 결과가 달라지는 동적 검색이므로 Resource가 아니라 Tool
- Resource runbook://incident-response/{incident_type} : URI로 식별되는 정적 문서
- Prompt  incident_analysis_prompt : 여러 Tool을 조합하는 표준 진단 절차를 강제
"""
import json
import random
import time
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from rag import get_index

mcp = FastMCP(name="capstone-all-total-ops")

DATA_DIR = Path(__file__).parent / "data"


class SystemStatus(BaseModel):
    system_name: str = Field(description="시스템 이름")
    status: str = Field(description="상태: healthy / degraded / down")
    cpu_percent: float = Field(description="CPU 사용률 (%)")
    memory_percent: float = Field(description="메모리 사용률 (%)")
    last_checked: str = Field(description="마지막 체크 시각 (ISO 8601)")
    alerts: list[str] = Field(default_factory=list, description="활성 알림 목록")


@mcp.tool()
def get_monitoring_status(system_name: str) -> SystemStatus:
    """지정된 시스템의 현재 모니터링 상태를 조회합니다 (호출마다 값이 달라지는 동적 Tool)."""
    monitoring_data = json.loads((DATA_DIR / "monitoring.json").read_text())
    if system_name not in monitoring_data:
        return SystemStatus(
            system_name=system_name, status="unknown",
            cpu_percent=0.0, memory_percent=0.0,
            last_checked=datetime.now().isoformat(),
            alerts=[f"System '{system_name}' not found in monitoring"],
        )
    data = monitoring_data[system_name]
    jitter = random.uniform(-3.0, 8.0)
    return SystemStatus(
        system_name=system_name,
        status=data["status"],
        cpu_percent=round(data["cpu_base"] + jitter, 1),
        memory_percent=round(data["memory_base"] + jitter / 2, 1),
        last_checked=datetime.now().isoformat(timespec="microseconds"),
        alerts=data.get("alerts", []),
    )


class RunbookHit(BaseModel):
    incident_type: str = Field(description="runbook 파일명(stem) — runbook:// URI에 그대로 쓴다")
    score: float = Field(description="정렬 점수 (vector=cosine, bm25=정규화 BM25, hybrid=RRF)")
    vector_score: float | None = Field(
        default=None, description="최대 cosine 유사도 (bm25 모드이거나 임베딩을 못 쓰면 null)")
    bm25_score: float = Field(description="질의 내 최댓값으로 정규화한 BM25 점수 [0,1]")
    snippet: str = Field(description="가장 잘 맞은 청크(증상 bullet) 일부")


class RunbookSearchResult(BaseModel):
    mode: str = Field(description="실제로 쓰인 검색 방식 (임베딩 실패 시 bm25)")
    degraded: bool = Field(description="임베딩 서버 장애로 BM25로 폴백했으면 true")
    hits: list[RunbookHit] = Field(default_factory=list, description="점수 내림차순 runbook 후보")
    elapsed_ms: float = Field(description="검색 소요 시간 (ms)")


@mcp.tool()
def search_runbook(query: str, top_k: int = 3, mode: str = "hybrid") -> RunbookSearchResult:
    """의미 기반 runbook 검색 — 증상·알림 문장으로 관련 runbook 후보를 점수순으로 반환합니다.
    질의마다 결과가 달라지는 동적 검색이므로 Resource가 아니라 Tool이다.
    mode: hybrid(임베딩+BM25 RRF, 기본) / vector(=rag) / bm25."""
    started = time.perf_counter()
    result = get_index().search(query, top_k=top_k, mode=mode)
    return RunbookSearchResult(**result, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))


@mcp.resource("runbook://incident-response/{incident_type}")
def get_incident_runbook(incident_type: str) -> str:
    """특정 incident 유형의 runbook(정적 문서)을 반환합니다."""
    runbook_path = DATA_DIR / "runbooks" / f"{incident_type}.md"
    if not runbook_path.exists():
        return f"# Runbook Not Found\n\nNo runbook for incident type: {incident_type}"
    return runbook_path.read_text()


@mcp.prompt()
def incident_analysis_prompt(system_name: str, symptom: str) -> str:
    """장애 진단 표준 절차 프롬프트 — planning 노드가 계획을 세울 때 이 절차를 따른다."""
    return f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 장애 정보
- 시스템: {system_name}
- 증상: {symptom}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()
