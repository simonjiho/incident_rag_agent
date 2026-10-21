"""Before/After 평가 스크립트 — runbook 선택 정확도(selector)와 에이전트 전체 흐름(e2e).

프로젝트 루트에서 .venv 파이썬으로 실행한다.
  python eval/run_eval.py selector               # 선택기 단독 (Ollama bge-m3 필요, 없으면 degraded로 기록)
  python eval/run_eval.py threshold [--sweep]    # RAG_MIN_SCORE 근거: dev split(앞 10건) 점수 분포
  python eval/run_eval.py e2e --base-url http://127.0.0.1:8100 --label hybrid-fake
  python eval/run_eval.py compare eval/results/e2e_a.json eval/results/e2e_b.json ...
결과는 eval/results/<kind>_..._<시각>.json 으로 저장하고, 표는 markdown으로 출력한다.
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ["LANGCHAIN_TRACING_V2"] = "false"   # 평가는 LangSmith에 기록하지 않는다
os.environ["LANGSMITH_TRACING"] = "false"

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.runbook_selector import (DEFAULT_MIN_SCORE, INCLUDE_QUERY, KeywordSelector,  # noqa: E402
                                    McpRagSelector, build_search_text)
from rag import OllamaEmbedder, RunbookIndex  # noqa: E402
from rag.index import DEFAULT_RUNBOOK_DIR  # noqa: E402

SELECTOR_CASES = ROOT / "eval" / "selector_cases.jsonl"
E2E_CASES = ROOT / "eval" / "e2e_cases.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
CATEGORIES = ("keyword", "paraphrase", "trap")
RAG_MODES = ("vector", "bm25", "hybrid")
# 개선 전 원본 data/monitoring.json에 있던 시스템 — e2e 정답 쌍을 '원본 시스템'과 '신규 시스템'으로
# 나눠 본다. 신규 9개 시스템의 유형은 대부분 키워드 규칙이 애초에 출력할 수 없는 유형이다.
BASE_SYSTEMS = {"auth-service", "web-01", "payments-service", "cache-service",
                "gateway-service", "log-service"}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def p95(values: list[float]) -> float:
    """selector 지연용 (40건). e2e 12건은 p95가 사실상 최댓값 근처라 중앙값·최댓값을 쓴다."""
    return float(np.percentile(values, 95)) if values else 0.0


def ratio(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.1%})" if n else "-"


def top3_cell(name: str, summary: dict) -> str:
    """키워드 규칙은 후보를 하나만 내므로 top-3가 정의되지 않는다."""
    return "-" if name == "keyword" else ratio(summary["top3"], summary["n"])


def save(kind: str, label: str, payload: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{kind}_{stamp}.json" if kind == "selector" else f"{kind}_{label}_{stamp}.json"
    path = RESULTS_DIR / name
    path.write_text(json.dumps({"kind": kind, "label": label, "created": stamp, **payload},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------- selector

def monitoring_of(case: dict) -> dict:
    """selector 케이스 → get_monitoring_status가 돌려주는 것과 같은 모양."""
    return {"system_name": case["system"], "status": case["status"], "alerts": case["alerts"]}


def fresh_index(mode: str) -> RunbookIndex:
    """방식마다 새 인덱스(=질의 캐시가 빈 임베더)로 재야 지연시간이 공정하다.
    첫 검색에서 청크 임베딩·모델 로딩이 일어나므로 측정 전에 한 번 데워 둔다."""
    index = RunbookIndex.from_dir(DEFAULT_RUNBOOK_DIR)
    index.search("warm-up", top_k=1, mode=mode)
    return index


def run_keyword(cases: list[dict]) -> list[dict]:
    """개선 전 규칙: status+alerts에 키워드 표를 적용 (KeywordSelector = classify_incident_type)."""
    selector, records = KeywordSelector(), []
    for case in cases:
        started = time.perf_counter()
        predicted = selector.pick(monitoring_of(case)).incident_type
        ms = (time.perf_counter() - started) * 1000
        records.append(record(case, predicted, [predicted], ms))
    return records


def run_rag(cases: list[dict], mode: str, include_query: bool = INCLUDE_QUERY,
            min_score: float | None = None) -> list[dict]:
    """RunbookIndex를 직접 불러 search_runbook Tool과 같은 검색을 한다 (MCP 왕복 시간은 제외).
    min_score를 주면 에이전트와 같은 McpRagSelector.decide 규칙(낮은 점수 → 키워드)을 적용한다."""
    index = fresh_index(mode)
    selector = McpRagSelector(mcp=None, mode=mode, min_score=min_score or 0.0)
    records = []
    for case in cases:
        monitoring = monitoring_of(case)
        text = build_search_text(case["query"], monitoring, include_query=include_query)
        started = time.perf_counter()
        result = index.search(text, top_k=3, mode=mode)
        ranked = [h["incident_type"] for h in result["hits"]]
        fallback = False
        if min_score is not None:
            selection = selector.decide(monitoring, result)
            fallback = selection.fallback
            ranked = list(dict.fromkeys([selection.incident_type] + ranked))
        ms = (time.perf_counter() - started) * 1000
        records.append(record(case, ranked[0] if ranked else None, ranked[:3], ms,
                              result["degraded"], fallback))
    return records


def record(case: dict, predicted, top3: list, ms: float, degraded: bool = False,
           fallback: bool = False) -> dict:
    return {"id": case["id"], "category": case["category"], "expected": case["expected"],
            "predicted": predicted, "top3": top3, "ms": round(ms, 3),
            "degraded": degraded, "fallback": fallback}


def summarize(records: list[dict]) -> dict:
    ms = [r["ms"] for r in records]
    return {"n": len(records),
            "top1": sum(r["predicted"] == r["expected"] for r in records),
            "top3": sum(r["expected"] in r["top3"] for r in records),
            "mean_ms": round(statistics.mean(ms), 3), "p95_ms": round(p95(ms), 3),
            "fallback": sum(r["fallback"] for r in records),
            "degraded": sum(r["degraded"] for r in records)}


def cmd_selector(args) -> None:
    cases = load_jsonl(SELECTOR_CASES)
    methods = {"keyword": run_keyword(cases)}
    for mode in RAG_MODES:
        methods[mode] = run_rag(cases, mode)
    methods["hybrid+fallback"] = run_rag(cases, "hybrid", min_score=args.min_score)

    results = {}
    for name, records in methods.items():
        results[name] = {"overall": summarize(records),
                         "by_category": {c: summarize([r for r in records if r["category"] == c])
                                         for c in CATEGORIES},
                         "cases": records}

    # 검색 텍스트에 사용자 질의를 넣을지 비교 (INCLUDE_QUERY 결정 근거)
    search_text = {}
    for mode in RAG_MODES:
        for include in (False, True):
            s = summarize(run_rag(cases, mode, include_query=include))
            search_text[f"{mode}|{'alerts+query' if include else 'alerts-only'}"] = s

    print(f"\n## runbook 선택기 평가 (selector_cases {len(cases)}건, "
          f"검색 텍스트={'alerts+query' if INCLUDE_QUERY else 'alerts-only'}, "
          f"min_score={args.min_score})\n")
    print("| 방식 | top-1 | top-3 | keyword top-1 | paraphrase top-1 | trap top-1 "
          "| 평균 ms | p95 ms | 폴백 | degraded |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, res in results.items():
        o, by = res["overall"], res["by_category"]
        cats = " | ".join(ratio(by[c]["top1"], by[c]["n"]) for c in CATEGORIES)
        print(f"| {name} | {ratio(o['top1'], o['n'])} | {top3_cell(name, o)} | {cats} "
              f"| {o['mean_ms']} | {o['p95_ms']} | {o['fallback']} | {o['degraded']} |")
    print("\n## 검색 텍스트 비교 (사용자 질의 포함 여부)\n")
    print("| 방식 | alerts-only top-1 | alerts+query top-1 | alerts-only top-3 | alerts+query top-3 |")
    print("|---|---|---|---|---|")
    for mode in RAG_MODES:
        a, q = search_text[f"{mode}|alerts-only"], search_text[f"{mode}|alerts+query"]
        print(f"| {mode} | {ratio(a['top1'], a['n'])} | {ratio(q['top1'], q['n'])} "
              f"| {ratio(a['top3'], a['n'])} | {ratio(q['top3'], q['n'])} |")

    config = {"cases": str(SELECTOR_CASES.relative_to(ROOT)), "n": len(cases),
              "include_query": INCLUDE_QUERY, "min_score": args.min_score,
              "embed_model": OllamaEmbedder(cache_dir=None).model}
    path = save("selector", "selector",
                {"config": config, "methods": results, "search_text": search_text})
    print(f"\n저장: {path.relative_to(ROOT)}")


def cmd_threshold(args) -> None:
    """dev split(앞 N건)의 hybrid top-1 vector_score와 정오를 보여 준다 — 임계값은 이것만 보고 정한다."""
    cases = load_jsonl(SELECTOR_CASES)
    dev = cases[:args.dev]
    index = fresh_index("hybrid")
    rows = []
    for case in dev:
        result = index.search(build_search_text(case["query"], monitoring_of(case)), 3, "hybrid")
        if result["degraded"]:   # cosine이 없으면 임계값 근거를 만들 수 없다
            sys.exit("threshold는 임베딩(bge-m3)이 필요합니다 — Ollama와 RAG_EMBED_MODEL을 확인하세요.")
        top = result["hits"][0]
        keyword = KeywordSelector().pick(monitoring_of(case)).incident_type
        rows.append((top["vector_score"], case["id"], case["category"], case["expected"],
                     top["incident_type"], keyword))
    print(f"\n## dev split (selector_cases 앞 {len(dev)}건) — hybrid top-1 vector_score\n")
    print("| vector_score | id | category | 정답 | RAG top-1 | 정오 | 키워드 규칙 |")
    print("|---|---|---|---|---|---|---|")
    for score, cid, cat, exp, top, kw in sorted(rows):
        print(f"| {score:.3f} | {cid} | {cat} | {exp} | {top} | {'O' if top == exp else 'X'} "
              f"| {kw} ({'O' if kw == exp else 'X'}) |")
    right = [r[0] for r in rows if r[4] == r[3]]
    wrong = [r[0] for r in rows if r[4] != r[3]]
    if right and wrong:
        verdict = "분리됨" if max(wrong) < min(right) else "겹침 (깔끔한 경계 없음)"
        print(f"\n정답 최저 {min(right):.3f} / 오답 최고 {max(wrong):.3f} → {verdict}")
    print(f"현재 DEFAULT_MIN_SCORE = {DEFAULT_MIN_SCORE}")

    if args.sweep:   # 사후 확인용 — 임계값 선택에는 쓰지 않는다 (test 쪽을 보고 고르면 과적합)
        print("\n## (사후 확인) 임계값별 hybrid+fallback top-1 — dev / 나머지\n")
        print("| min_score | dev top-1 | 나머지 top-1 | 폴백 수(전체) |")
        print("|---|---|---|---|")
        for t in args.sweep_values:
            records = run_rag(cases, "hybrid", min_score=t)
            d, rest = summarize(records[:args.dev]), summarize(records[args.dev:])
            print(f"| {t:.2f} | {ratio(d['top1'], d['n'])} | {ratio(rest['top1'], rest['n'])} "
                  f"| {d['fallback'] + rest['fallback']} |")


# ---------------------------------------------------------------- e2e

def run_case(client: httpx.Client, base_url: str, case: dict, thread_id: str) -> dict:
    """POST /chat 한 번 — 읽은 runbook = TaskCompleted(source=runbook) 이벤트의 target.
    개선 전 서버(runbook_selections 없음)에도 그대로 쓸 수 있게 이벤트만 본다."""
    expected = case["expected"]
    started = time.perf_counter()
    try:
        resp = client.post(f"{base_url}/chat", json={"message": case["query"],
                                                     "thread_id": thread_id})
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as e:
        return {"id": case["id"], "error": str(e), "expected": expected, "read": [],
                "recall": 0.0, "hits": 0, "exact": False, "extra": [],
                "system_hits": {system: False for system in expected},
                "latency_s": round(time.perf_counter() - started, 3)}
    latency = time.perf_counter() - started
    read = sorted({e["target"] for e in body["events"]
                   if e.get("type") == "TaskCompleted" and e.get("source") == "runbook"})
    want = set(expected.values())
    system_hits = {system: t in read for system, t in expected.items()}   # 그 시스템 정답을 읽었나
    meta = body["metadata"]
    selections = meta.get("runbook_selections")
    pairs_ok = None
    if selections is not None:   # 개선 후 서버만 — 시스템별로 맞는 runbook을 골랐는지
        chosen = {s["system"]: s["incident_type"] for s in selections if s["incident_type"]}
        pairs_ok = sum(chosen.get(system) == t for system, t in expected.items())
    return {"id": case["id"], "expected": expected, "read": read,
            "hits": len(want & set(read)), "recall": round(len(want & set(read)) / len(want), 3),
            "exact": set(read) == want, "extra": sorted(set(read) - want),
            "pairs_ok": pairs_ok, "system_hits": system_hits, "latency_s": round(latency, 3),
            "cost_usd": meta.get("cost_usd", 0.0), "retry_count": meta.get("retry_count", 0),
            "validation_attempts": meta.get("validation_attempts", 0),
            "replan_rounds": meta.get("replan_rounds", 0),
            "selections": [{k: s[k] for k in ("system", "incident_type", "method", "score",
                                              "fallback")} for s in selections or []],
            "reply": body["reply"]}


def group_hits(records: list[dict], base: bool) -> str:
    """정답 (시스템, 유형) 쌍 중 원본 시스템(base=True) 또는 신규 시스템 쪽만 센 적중 수 'k/n'."""
    hits = [hit for r in records for system, hit in r["system_hits"].items()
            if (system in BASE_SYSTEMS) == base]
    return f"{sum(hits)}/{len(hits)}"


def summarize_e2e(records: list[dict], cases: list[dict]) -> dict:
    ok = [r for r in records if "error" not in r]
    latency = [r["latency_s"] for r in records]
    total_pairs = sum(len(c["expected"]) for c in cases)
    pairs = [r["pairs_ok"] for r in ok if r.get("pairs_ok") is not None]
    return {
        "cases": len(records), "errors": len(records) - len(ok),
        "mean_recall": round(statistics.mean(r["recall"] for r in records), 3),
        "micro_recall": round(sum(r["hits"] for r in records) / total_pairs, 3),
        "hits": sum(r["hits"] for r in records), "expected_total": total_pairs,
        "hits_base_systems": group_hits(records, base=True),
        "hits_new_systems": group_hits(records, base=False),
        "exact_rate": round(sum(r["exact"] for r in records) / len(records), 3),
        "pair_accuracy": round(sum(pairs) / total_pairs, 3) if len(pairs) == len(records) else None,
        "extra_runbooks": sum(len(r["extra"]) for r in records),
        "latency_mean_s": round(statistics.mean(latency), 3),
        "latency_median_s": round(statistics.median(latency), 3),
        "latency_max_s": round(max(latency), 3),
        "cost_usd_total": round(sum(r.get("cost_usd", 0.0) for r in ok), 6),
        "retries_total": sum(r.get("retry_count", 0) for r in ok),
        "validation_attempts_mean": round(statistics.mean(
            r.get("validation_attempts", 0) for r in ok), 3) if ok else None,
        "replan_rate": round(sum(r.get("replan_rounds", 0) > 0 for r in ok) / len(ok), 3)
        if ok else None,
    }


def cmd_e2e(args) -> None:
    cases = load_jsonl(E2E_CASES)
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    base_url = args.base_url.rstrip("/")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with httpx.Client(timeout=args.timeout) as client:
        health = client.get(f"{base_url}/health").json()
        records = [run_case(client, base_url, c, f"eval-{args.label}-{stamp}-{c['id']}")
                   for c in cases]
    summary = summarize_e2e(records, cases)

    print(f"\n## e2e — {args.label} ({base_url}, backend={health.get('llm_backend')}, "
          f"selector={health.get('runbook_selector', '(없음: 개선 전)')})\n")
    print("| id | 정답 유형 | 읽은 runbook | recall | exact | 지연(s) |")
    print("|---|---|---|---|---|---|")
    for r in records:
        print(f"| {r['id']} | {', '.join(sorted(set(r['expected'].values())))} "
              f"| {', '.join(r['read']) or '-'} | {r['recall']:.2f} | {'O' if r['exact'] else 'X'} "
              f"| {r['latency_s']:.2f} |")
    print_e2e_table({args.label: summary})
    path = save("e2e", args.label, {"base_url": base_url, "health": health,
                                    "summary": summary, "cases": records})
    print(f"\n저장: {path.relative_to(ROOT)}")


E2E_ROWS = [("cases", "케이스 수"), ("mean_recall", "runbook recall (케이스 평균)"),
            ("micro_recall", "runbook recall (전체 정답 유형 기준)"),
            ("hits_base_systems", "적중 쌍 — 원본 시스템 (개선 전에도 있던 5개)"),
            ("hits_new_systems", "적중 쌍 — 신규 시스템 (증강한 9개)"),
            ("exact_rate", "정확히 일치한 비율"),
            ("pair_accuracy", "시스템별 정답 비율 (개선 후만)"),
            ("extra_runbooks", "불필요하게 읽은 runbook 수"),
            ("latency_mean_s", "지연 평균 (s)"), ("latency_median_s", "지연 중앙값 (s)"),
            ("latency_max_s", "지연 최대 (s)"),
            ("cost_usd_total", "비용 합계 (USD, 가상 단가)"), ("retries_total", "재시도 합계"),
            ("validation_attempts_mean", "검증 시도 평균"), ("replan_rate", "재계획 비율"),
            ("errors", "요청 오류")]


def print_e2e_table(summaries: dict[str, dict]) -> None:
    labels = list(summaries)
    print("\n| 지표 | " + " | ".join(labels) + " |")
    print("|---|" + "---|" * len(labels))
    for key, title in E2E_ROWS:
        cells = []
        for label in labels:
            value = summaries[label].get(key)
            if key == "micro_recall" and value is not None:
                value = f"{value} ({summaries[label]['hits']}/{summaries[label]['expected_total']})"
            cells.append("-" if value is None else str(value))
        print(f"| {title} | " + " | ".join(cells) + " |")


# ---------------------------------------------------------------- compare

def cmd_compare(args) -> None:
    files = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.files]
    e2e = {f["label"]: f["summary"] for f in files if f["kind"] == "e2e"}
    if e2e:
        print("\n## e2e Before/After")
        print_e2e_table(e2e)
    selectors = [f for f in files if f["kind"] == "selector"]
    if selectors:
        print("\n## selector\n")
        print("| 결과 파일 | 방식 | top-1 | top-3 | 평균 ms | p95 ms |")
        print("|---|---|---|---|---|---|")
        for f in selectors:
            for name, res in f["methods"].items():
                o = res["overall"]
                print(f"| {f['created']} | {name} | {ratio(o['top1'], o['n'])} "
                      f"| {top3_cell(name, o)} | {o['mean_ms']} | {o['p95_ms']} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("selector", help="runbook 선택기 단독 평가 (selector_cases.jsonl)")
    p.add_argument("--min-score", type=float,
                   default=float(os.environ.get("RAG_MIN_SCORE", DEFAULT_MIN_SCORE)))
    p.set_defaults(func=cmd_selector)

    p = sub.add_parser("threshold", help="RAG_MIN_SCORE 근거 — dev split 점수 분포")
    p.add_argument("--dev", type=int, default=10, help="dev split 크기 (앞에서부터)")
    p.add_argument("--sweep", action="store_true", help="(사후 확인) 임계값별 정확도도 출력")
    p.add_argument("--sweep-values", type=float, nargs="+",
                   default=[0.0, 0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62])
    p.set_defaults(func=cmd_threshold)

    p = sub.add_parser("e2e", help="실행 중인 서버에 e2e_cases.jsonl을 POST /chat")
    p.add_argument("--base-url", default="http://127.0.0.1:8100")
    p.add_argument("--label", default="run")
    p.add_argument("--ids", help="일부 케이스만 (예: e01,e03)")
    p.add_argument("--timeout", type=float, default=600.0, help="요청당 타임아웃 (s)")
    p.set_defaults(func=cmd_e2e)

    p = sub.add_parser("compare", help="결과 JSON 2개 이상을 Before/After 표로")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
