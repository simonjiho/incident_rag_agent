"""평가 데이터 정합성 검사 — runbook / monitoring.json / selector_cases / e2e_cases.

실행: .venv/bin/python eval/tools/check_data.py   (통과하면 exit 0, 실패하면 exit 1)

검사 항목
1. runbook 14개의 형식 (제목 / ## 증상 6~10개 / ## 조치 절차 번호 줄) + 기존 6개 원문 유지
2. monitoring.json (기존 6개 유지, 신규 9개, 키워드 규칙 예측 vs 정답)
3. selector_cases 40건 (분포, 키워드 규칙 sanity, 질의에 유형명 노출 여부)
4. e2e_cases 12건 (시스템 존재, healthy 제외, 라우터 단어, 다중 시스템 구성)
5. anti-leakage: alerts/query 와 runbook 본문 사이의 최장 공통 부분문자열 < 20자
"""
import json
import os
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = Path(os.environ.get("BASE_DIR", ROOT.parent / "all_total_vue_student"))   # 개선 전 원본 (읽기 전용 기준선)
HAS_BASE = BASE_DIR.exists()   # 원본이 없으면(저장소만 받은 경우) 원본 대조는 건너뛴다
RUNBOOK_DIR = ROOT / "data" / "runbooks"
LCS_LIMIT = 20

OLD_TYPES = ["db-down", "cpu-spike", "disk-full", "high-latency", "memory-leak", "network-timeout"]
NEW_TYPES = ["cert-expiry", "dns-failure", "db-lock", "queue-backlog",
             "rate-limit", "config-error", "thread-pool-exhaustion", "replication-lag"]
ALL_TYPES = OLD_TYPES + NEW_TYPES

# 원본 agent/graph.py 의 _INCIDENT_TYPE_RULES 복사본 (check_rules_copy 가 원본과 대조) — 순서가 중요하다 (먼저 맞는 키워드가 이김)
RULES = [("latency", "high-latency"), ("pool", "high-latency"), ("cpu", "cpu-spike"),
         ("memory", "memory-leak"), ("heap", "memory-leak"), ("timeout", "network-timeout"),
         ("network", "network-timeout"), ("disk", "disk-full"), ("down", "db-down")]

# monitoring.json 각 시스템의 정답 유형 (알림 내용이 실제로 가리키는 원인). web-01 은 healthy
SYSTEM_TRUTH = {
    "auth-service": "high-latency", "payments-service": "cpu-spike", "cache-service": "memory-leak",
    "gateway-service": "network-timeout", "log-service": "disk-full",
    "ledger-service": "db-lock", "portal-service": "cert-expiry", "search-service": "dns-failure",
    "notification-service": "queue-backlog", "shipping-service": "rate-limit",
    "invoice-service": "config-error", "inventory-service": "thread-pool-exhaustion",
    "reporting-service": "replication-lag", "recommend-service": "memory-leak",
}
OLD_SYSTEMS = ["auth-service", "web-01", "payments-service", "cache-service", "gateway-service", "log-service"]

# 질의(query)에 노출되면 안 되는 유형별 단어 — 정답을 질의에서 바로 읽을 수 있기 때문
LEAK_WORDS = {
    "db-down": ["db-down", "db down", "다운"], "cpu-spike": ["cpu-spike", "cpu"],
    "disk-full": ["disk-full", "디스크"], "high-latency": ["high-latency", "지연"],
    "memory-leak": ["memory-leak", "메모리", "누수", "oom"], "network-timeout": ["network-timeout", "타임아웃", "네트워크"],
    "cert-expiry": ["cert-expiry", "인증서", "만료"], "dns-failure": ["dns-failure", "dns"],
    "db-lock": ["db-lock", "락", "lock"], "queue-backlog": ["queue-backlog", "적체", "backlog", "큐"],
    "rate-limit": ["rate-limit", "rate limit", "호출 제한", "제한 초과"], "config-error": ["config-error", "설정 오류"],
    "thread-pool-exhaustion": ["thread-pool", "스레드", "고갈"], "replication-lag": ["replication-lag", "복제"],
}

ROUTER_RE = re.compile(r"장애|진단|다운|지연|급증|incident|티켓")          # fake LLM route() 와 동일
SYSTEM_RE = re.compile(r"[a-z][a-z0-9-]+-(?:service|\d+)")                  # fake LLM plan() 과 동일

errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def rule_predict(status: str, alerts: list[str]) -> str:
    """기존 키워드 규칙: status+alerts 를 소문자로 합친 뒤 첫 번째로 맞는 키워드의 유형."""
    text = f"{status} " + " ".join(alerts)
    text = text.lower()
    for keyword, incident_type in RULES:
        if keyword in text:
            return incident_type
    return "db-down"   # 폴백


def rule_keywords_in(status: str, alerts: list[str]) -> list[str]:
    text = (f"{status} " + " ".join(alerts)).lower()
    return [k for k, _ in RULES if k in text]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def norm(text: str) -> str:
    """LCS 비교용 정규화 — 소문자, 마크다운 기호 제거, 공백 정리 (일부러 엄격하게)."""
    return re.sub(r"\s+", " ", re.sub(r"[`*]", "", text.lower())).strip()


def lcs(a: str, b: str) -> str:
    a, b = norm(a), norm(b)
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return a[m.a:m.a + m.size]


def max_lcs(texts: list[str], runbooks: dict[str, str]) -> tuple[int, str, str]:
    """texts 의 각 문장을 모든 runbook 본문과 비교해 (최대 길이, 공통 문자열, runbook 이름)."""
    best = (0, "", "")
    for t in texts:
        for name, body in runbooks.items():
            s = lcs(t, body)
            if len(s) > best[0]:
                best = (len(s), s, name)
    return best


# --------------------------------------------------------------------------- 1. runbook
def check_rules_copy() -> None:
    """위 RULES 가 개선 전 원본 agent/graph.py 의 _INCIDENT_TYPE_RULES 와 같은지 대조한다."""
    if not HAS_BASE:
        print(f"[skip] 원본 폴더가 없어 RULES 원본 대조를 건너뜁니다: {BASE_DIR}")
        return
    src = (BASE_DIR / "agent" / "graph.py").read_text(encoding="utf-8")
    m = re.search(r"_INCIDENT_TYPE_RULES = \[(.*?)\n\]", src, re.S)
    base_rules = re.findall(r'\("([^"]+)", "([^"]+)"\)', m.group(1)) if m else None
    check(base_rules == RULES, f"RULES 가 원본 graph.py 와 다름: {base_rules}")


def check_runbooks() -> dict[str, str]:
    runbooks = {}
    files = sorted(RUNBOOK_DIR.glob("*.md"))
    check([f.stem for f in files] == sorted(ALL_TYPES), f"runbook 파일 목록 불일치: {[f.stem for f in files]}")
    for f in files:
        name, text = f.stem, f.read_text(encoding="utf-8")
        runbooks[name] = text
        lines = text.rstrip("\n").split("\n")
        check(lines[0].startswith("# Runbook: "), f"{name}: 제목 줄 형식")
        if name in NEW_TYPES:
            check(bool(re.match(r"^# Runbook: .+\(.+\) 대응 절차$", lines[0])), f"{name}: 신규 제목 형식")
        check(lines[1] == "" and lines[2] == "## 증상", f"{name}: '## 증상' 위치")
        idx = lines.index("## 조치 절차") if "## 조치 절차" in lines else -1
        check(idx > 0 and lines[idx - 1] == "", f"{name}: '## 조치 절차' 위치/앞 빈 줄")
        bullets = lines[3:idx - 1] if idx > 0 else []
        check(6 <= len(bullets) <= 10, f"{name}: 증상 bullet {len(bullets)}개 (6~10 필요)")
        check(all(b.startswith("- ") for b in bullets), f"{name}: bullet 형식")
        steps = lines[idx + 1:] if idx > 0 else []
        check(all(re.match(r"^\d+\.\s+", s) for s in steps), f"{name}: 번호 줄 형식")
        check([int(s.split(".")[0]) for s in steps] == list(range(1, len(steps) + 1)), f"{name}: 번호 연속성")
        # fake LLM 이 파싱하는 방식과 동일하게 세어 본다 (빈 줄 제외, 첫 줄은 제목)
        body = [l.strip() for l in text.strip().splitlines() if l.strip()]
        parsed = [m.group(1) for l in body[1:] if (m := re.match(r"^\d+\.\s+(.*)", l))]
        check(len(parsed) == len(steps), f"{name}: fake LLM 파싱 단계 수 불일치")
        if name in NEW_TYPES:
            check(len(steps) == 5, f"{name}: 신규 절차는 5단계 (현재 {len(steps)})")
            check(bool(steps) and "get_monitoring_status" in steps[0], f"{name}: 1단계에 get_monitoring_status")
    # 기존 6개: 제목과 번호 줄이 원본과 글자 단위로 같아야 한다
    for name in (OLD_TYPES if HAS_BASE else []):
        base_file = BASE_DIR / "data" / "runbooks" / f"{name}.md"
        if not base_file.exists() or name not in runbooks:
            check(False, f"{name}: 원본 비교 불가 ({base_file})")
            continue
        base_lines = [l for l in base_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        new_lines = [l for l in runbooks[name].splitlines() if re.match(r"^\d+\.\s+", l)]
        check(base_lines[0] == runbooks[name].splitlines()[0], f"{name}: 제목 줄이 원본과 다름")
        check(base_lines[1:] == new_lines, f"{name}: 번호 단계가 원본과 다름")
    return runbooks


# --------------------------------------------------------------------------- 2. monitoring
def check_monitoring(runbooks: dict[str, str]) -> dict:
    mon = json.loads((ROOT / "data" / "monitoring.json").read_text(encoding="utf-8"))
    if HAS_BASE:
        base = json.loads((BASE_DIR / "data" / "monitoring.json").read_text(encoding="utf-8"))
        check(all(k in mon and mon[k] == v for k, v in base.items()), "기존 6개 시스템이 원본과 다름")
        check(list(mon)[:len(base)] == list(base), "기존 6개 시스템의 순서가 바뀜")
    else:
        base = dict.fromkeys(list(mon)[:6])   # 원본이 없으면 앞의 6개를 기존 시스템으로 본다
    new = [k for k in mon if k not in base]
    check(len(new) == 9, f"신규 시스템 {len(new)}개 (9 필요)")
    check(all(re.fullmatch(r"[a-z][a-z0-9-]+-service", k) for k in new), "신규 시스템 이름 형식")
    names = list(mon)
    for a in names:   # ollama/openai 백엔드는 'in query' 부분문자열 매칭을 쓰므로 이름끼리 포함 관계면 오탐
        for b in names:
            check(a == b or a not in b, f"시스템 이름 포함 관계: {a} in {b}")
    check(sum(mon[k]["status"] == "down" for k in new) <= 1, "신규 중 down 상태는 최대 1개")
    check(set(SYSTEM_TRUTH) | {"web-01"} == set(mon), "SYSTEM_TRUTH 키와 monitoring.json 키 불일치")
    check(sorted(SYSTEM_TRUTH[k] for k in new) == sorted(NEW_TYPES + ["memory-leak"]),
          "신규 9개 시스템의 정답 유형 구성 (신규 8유형 + memory-leak 1)")
    for k in new:
        e = mon[k]
        check(1 <= len(e["alerts"]) <= 2, f"{k}: alerts 1~2개")
        check(e["status"] in ("degraded", "down"), f"{k}: status")
        check(isinstance(e["cpu_base"], float) and isinstance(e["memory_base"], float), f"{k}: cpu/memory_base float")
    oom = [k for k in new if SYSTEM_TRUTH[k] == "memory-leak"][0]
    oom_text = " ".join(mon[oom]["alerts"]).lower()
    check("oomkilled" in oom_text and "memory" not in oom_text and "heap" not in oom_text,
          f"{oom}: OOMKilled 이면서 memory/heap 단어가 없어야 함")
    print("\n[monitoring] 시스템별 키워드 규칙 예측 (신규 9개는 규칙이 틀려야 함)")
    wrong_new = 0
    for k in mon:
        if k == "web-01":
            continue
        pred = rule_predict(mon[k]["status"], mon[k]["alerts"])
        hit = rule_keywords_in(mon[k]["status"], mon[k]["alerts"])
        mark = "맞음" if pred == SYSTEM_TRUTH[k] else "틀림"
        wrong_new += (k in new and pred != SYSTEM_TRUTH[k])
        print(f"  {k:22s} 정답={SYSTEM_TRUTH[k]:22s} 규칙={pred:16s} {mark}  키워드={hit or '없음'}")
    check(wrong_new >= 6, f"신규 중 규칙이 틀리는 시스템 {wrong_new}개 (6 이상 필요)")
    print(f"  -> 신규 9개 중 규칙 오답 {wrong_new}개")
    return mon


# --------------------------------------------------------------------------- 3. selector
def check_selector(runbooks: dict[str, str]) -> None:
    cases = load_jsonl(ROOT / "eval" / "selector_cases.jsonl")
    check(len(cases) == 40, f"selector 케이스 {len(cases)}건 (40 필요)")
    check([c["id"] for c in cases] == [f"s{i:02d}" for i in range(1, 41)], "selector id s01..s40 순서")
    cat = Counter(c["category"] for c in cases)
    check(cat == {"keyword": 12, "paraphrase": 18, "trap": 10}, f"카테고리 분포 {dict(cat)}")
    per_type = Counter(c["expected"] for c in cases)
    check(all(per_type[t] >= 2 for t in ALL_TYPES), f"모든 유형 2건 이상 필요: {dict(per_type)}")
    check(set(per_type) <= set(ALL_TYPES), "정의되지 않은 유형 사용")

    hit = Counter()          # 카테고리별 규칙 정답 수
    worst = (0, "", "", "")
    for c in cases:
        cid = c["id"]
        check(set(c) == {"id", "system", "status", "alerts", "query", "expected", "category"}, f"{cid}: 필드 구성")
        check(c["status"] in ("degraded", "down") and c["alerts"], f"{cid}: status/alerts")
        pred = rule_predict(c["status"], c["alerts"])
        kws = rule_keywords_in(c["status"], c["alerts"])
        hit[c["category"]] += pred == c["expected"]
        if c["category"] == "keyword":
            check(pred == c["expected"], f"{cid}: keyword 케이스인데 규칙 오답 (규칙={pred})")
            check(bool(kws), f"{cid}: keyword 케이스에 키워드 없음")
        elif c["category"] == "paraphrase":
            check(c["expected"] != "db-down", f"{cid}: paraphrase 정답이 db-down")
            check(not kws and c["status"] == "degraded", f"{cid}: paraphrase 에 규칙 키워드 포함 {kws}")
            check(pred != c["expected"], f"{cid}: paraphrase 인데 규칙 정답")
        else:
            check(bool(kws) and pred != c["expected"], f"{cid}: trap 은 키워드가 있고 규칙이 틀려야 함 ({kws}, {pred})")
        low_q = c["query"].lower()
        for w in LEAK_WORDS[c["expected"]]:
            check(w not in low_q, f"{cid}: 질의에 유형 노출 단어 '{w}'")
        n, s, rb = max_lcs(c["alerts"] + [c["query"]], runbooks)
        check(n < LCS_LIMIT, f"{cid}: runbook 과 공통 부분문자열 {n}자 '{s}' ({rb})")
        if n > worst[0]:
            worst = (n, cid, s, rb)

    print("\n[selector] 카테고리 분포:", dict(cat))
    print("[selector] 유형별 분포:", {t: per_type[t] for t in ALL_TYPES})
    print("[selector] 키워드 규칙 정답 수:", {k: f"{hit[k]}/{cat[k]}" for k in ("keyword", "paraphrase", "trap")},
          f"-> 전체 {sum(hit.values())}/40")
    print(f"[leakage] selector 최대 공통 부분문자열 {worst[0]}자 (기준 <{LCS_LIMIT}) "
          f"{worst[1]} '{worst[2]}' ~ {worst[3]}")


# --------------------------------------------------------------------------- 4. e2e
def check_e2e(mon: dict, runbooks: dict[str, str]) -> None:
    cases = load_jsonl(ROOT / "eval" / "e2e_cases.jsonl")
    check(len(cases) == 12, f"e2e 케이스 {len(cases)}건 (12 필요)")
    check([c["id"] for c in cases] == [f"e{i:02d}" for i in range(1, 13)], "e2e id e01..e12 순서")
    multi_diff = web_cases = old_only = 0
    pair_total = pair_hit = 0
    for c in cases:
        cid, q, exp = c["id"], c["query"], c["expected"]
        check(bool(ROUTER_RE.search(q)), f"{cid}: 라우터 단어(장애|진단|지연|급증|다운) 없음")
        mentioned = SYSTEM_RE.findall(q)
        check(all(m in mon for m in mentioned), f"{cid}: monitoring.json 에 없는 시스템 언급 {mentioned}")
        for s, t in exp.items():
            check(s in mon and mon[s]["status"] != "healthy", f"{cid}: {s} 는 존재하는 비정상 시스템이어야 함")
            check(s in q, f"{cid}: 질의에 {s} 이름이 없음")
            check(SYSTEM_TRUTH.get(s) == t, f"{cid}: {s} 정답 {t} != 알림이 가리키는 유형 {SYSTEM_TRUTH.get(s)}")
        for s in mentioned:
            if mon[s]["status"] == "healthy":
                check(s not in exp, f"{cid}: healthy 시스템 {s} 가 expected 에 있음")
        web_cases += "web-01" in mentioned
        multi_diff += len(exp) >= 2 and len(set(exp.values())) >= 2
        old_only += all(s in OLD_SYSTEMS for s in exp)
        # 개선 전(Before) 기준선: 규칙은 질의된 모든 시스템(+web-01)의 알림을 합쳐 runbook 을 '하나만' 고른다
        queried = list(dict.fromkeys(mentioned + ["web-01"]))
        merged_status = " ".join(mon[s]["status"] for s in queried)
        merged_alerts = [a for s in queried for a in mon[s]["alerts"]]
        pred = rule_predict(merged_status, merged_alerts)
        pair_total += len(exp)
        pair_hit += sum(t == pred for t in exp.values())
    check(multi_diff >= 4, f"유형이 서로 다른 다중 시스템 케이스 {multi_diff}개 (4 필요)")
    check(web_cases >= 2, f"web-01 언급 케이스 {web_cases}개 (2 필요)")
    check(old_only >= 3, f"기존 6개 시스템만 쓰는 케이스 {old_only}개 (3 필요)")
    n, s, rb = max_lcs([c["query"] for c in cases], runbooks)
    check(n < LCS_LIMIT, f"e2e 질의 공통 부분문자열 {n}자 '{s}' ({rb})")
    n2, s2, rb2 = max_lcs([a for v in mon.values() for a in v["alerts"]], runbooks)
    check(n2 < LCS_LIMIT, f"monitoring alerts 공통 부분문자열 {n2}자 '{s2}' ({rb2})")
    print(f"\n[e2e] 12건: 다중(유형 상이) {multi_diff}, web-01 언급 {web_cases}, 기존 시스템만 {old_only}, "
          f"(시스템,유형) 쌍 {pair_total}개")
    print(f"[e2e] 개선 전 규칙(유형 1개 선택) 기대 정답: {pair_hit}/{pair_total} 쌍")
    print(f"[leakage] e2e 질의 최대 {n}자 '{s}' ({rb}) / monitoring alerts 최대 {n2}자 '{s2}' ({rb2})")


def main() -> int:
    check_rules_copy()
    runbooks = check_runbooks()
    mon = check_monitoring(runbooks)
    check_selector(runbooks)
    check_e2e(mon, runbooks)
    print()
    if errors:
        print(f"FAIL — {len(errors)}건")
        for e in errors:
            print("  -", e)
        return 1
    print("PASS — 모든 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
