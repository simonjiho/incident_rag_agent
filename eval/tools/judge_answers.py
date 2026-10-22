"""LLM-as-judge — e2e 결과 파일의 최종 답변을 다른 모델(기본 Claude Sonnet 5.5)이 5점 척도로 채점한다.

e2e 평가는 '정답 runbook을 읽었는가'만 센다. 이 스크립트는 그 답변이 실제로 쓸 만한지 본다.
심사 모델에는 정답 runbook 본문과 해당 시스템의 모니터링 수치를 기준으로 주고, 답변이 어느
버전(개선 전/후)에서 나왔는지는 알려 주지 않는다. 답변을 쓴 모델(Haiku)과 다른 모델로 심사해
자기 채점 편향을 줄인다.

사용: .venv/bin/python eval/tools/judge_answers.py eval/results/e2e_before-haiku_*.json eval/results/e2e_after-haiku_*.json
키는 .env의 ANTHROPIC_API_KEY를 SDK가 읽는다 (출력하지 않음). 실제 과금 호출이다.
"""
import asyncio
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=ROOT / ".env")
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import anthropic  # noqa: E402

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-5-5")
MONITORING = json.loads((ROOT / "data" / "monitoring.json").read_text(encoding="utf-8"))
RUNBOOK_DIR = ROOT / "data" / "runbooks"

SYSTEM = """당신은 ICT 운영 장애 대응 보고서를 채점하는 심사관이다.
기준 자료(정답 runbook과 모니터링 수치)만 근거로, 답변이 운영자에게 실제로 도움이 되는지 채점하라.

점수 (score, 1~5)
5: 장애 시스템마다 정답 runbook에 맞는 대응 절차를 제시하고, 수치는 모니터링 값과 일치하며 지어낸 내용이 없다
4: 대체로 맞지만 한 시스템의 절차가 빈약하거나 사소한 누락이 있다
3: 일부 시스템만 맞는 절차이거나, 절차가 runbook과 무관한 일반론에 가깝다
2: 대부분 엉뚱한 장애 유형의 절차다
1: 장애 유형을 잘못 짚었고 조치도 틀렸거나, 수치를 지어냈다

systems_correct: 정답 runbook에 맞는 절차를 받은 장애 시스템 수
fabricated: 모니터링 범위를 벗어난 수치나 기준 자료에 없는 사실을 지어냈으면 true
reason: 판단 근거 한 문장 (한국어)"""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "systems_correct": {"type": "integer"},
        "fabricated": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["score", "systems_correct", "fabricated", "reason"],
    "additionalProperties": False,
}


def reference(expected: dict) -> str:
    parts = []
    for system, incident_type in expected.items():
        m = MONITORING.get(system, {})
        # 모니터링 Tool은 호출마다 흔들림을 더한다 (mcp_server.get_monitoring_status: CPU −3~+8, 메모리 −1.5~+4)
        cpu, mem = m.get("cpu_base", 0), m.get("memory_base", 0)
        parts.append(f"### {system} — 정답 유형 {incident_type}\n"
                     f"모니터링: 상태={m.get('status')}, CPU {cpu - 3:.1f}~{cpu + 8:.1f}% 범위, "
                     f"메모리 {mem - 1.5:.1f}~{mem + 4:.1f}% 범위 (호출마다 이 범위에서 달라지므로 "
                     f"범위 안의 값은 지어낸 것이 아니다), 알림 {m.get('alerts')}\n"
                     f"정답 runbook:\n{(RUNBOOK_DIR / f'{incident_type}.md').read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts)


async def judge_one(client, sem, case: dict) -> dict:
    user = (f"## 사용자 요청\n{case['query']}\n\n## 기준 자료 (장애 시스템 {len(case['expected'])}개)\n"
            f"{reference(case['expected'])}\n\n## 채점할 답변\n{case['reply']}")
    async with sem:
        msg = await client.messages.create(
            model=JUDGE_MODEL, max_tokens=6000, system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": SCHEMA}})
    text = "".join(b.text for b in msg.content if b.type == "text")
    verdict = json.loads(text) if msg.stop_reason != "refusal" and text else {
        "score": None, "systems_correct": None, "fabricated": None, "reason": "심사 거부"}
    return {"id": case["id"], "expected": case["expected"], **verdict,
            "usage": {"input": msg.usage.input_tokens, "output": msg.usage.output_tokens}}


async def judge_file(client, sem, path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    queries = {json.loads(l)["id"]: json.loads(l)["query"]
               for l in (ROOT / "eval" / "e2e_cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    cases = [{**c, "query": queries[c["id"]]} for c in data["cases"] if c.get("reply")]
    verdicts = await asyncio.gather(*(judge_one(client, sem, c) for c in cases))
    scores = [v["score"] for v in verdicts if v["score"] is not None]
    pairs = sum(len(v["expected"]) for v in verdicts)
    return {"label": data["label"], "source": path.name, "judge_model": JUDGE_MODEL,
            "summary": {"cases": len(verdicts), "mean_score": round(statistics.mean(scores), 2),
                        "score_counts": {s: scores.count(s) for s in range(5, 0, -1)},
                        "systems_correct": f"{sum(v['systems_correct'] or 0 for v in verdicts)}/{pairs}",
                        "fabricated": sum(bool(v["fabricated"]) for v in verdicts)},
            "verdicts": sorted(verdicts, key=lambda v: v["id"])}


async def main(paths: list[Path]) -> None:
    client = anthropic.AsyncAnthropic(max_retries=2, timeout=180.0)
    sem = asyncio.Semaphore(6)
    results = await asyncio.gather(*(judge_file(client, sem, p) for p in paths))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"\n## 답변 품질 (LLM-as-judge, {JUDGE_MODEL})\n")
    print("| 지표 | " + " | ".join(r["label"] for r in results) + " |")
    print("|---|" + "---|" * len(results))
    for key, name in [("mean_score", "평균 점수 (5점)"), ("score_counts", "점수 분포 5·4·3·2·1"),
                      ("systems_correct", "맞는 절차를 받은 시스템"), ("fabricated", "수치 지어냄 (건)")]:
        cells = []
        for r in results:
            v = r["summary"][key]
            cells.append(" · ".join(str(n) for n in v.values()) if isinstance(v, dict) else str(v))
        print(f"| {name} | " + " | ".join(cells) + " |")
    for r in results:
        out = ROOT / "eval" / "results" / f"judge_{r['label']}_{stamp}.json"
        out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {out.relative_to(ROOT)}")
        for v in r["verdicts"]:
            print(f"  {v['id']} {v['score']}점 — {v['reason']}")


if __name__ == "__main__":
    asyncio.run(main([Path(p) for p in sys.argv[1:]]))
