"""LLM_BACKEND=anthropic — Claude API 백엔드 (기본 claude-haiku-5-5).

fake_llm·ollama_llm·openai_llm과 같은 인터페이스(route·plan·analyze·chat·write)를 맞춘다.
로컬 Ollama는 이 노트북(16GB)에서 qwen3:8b 한 번 돌리면 요청 하나에 100초 가까이 걸려,
실모델 평가를 빠르게 돌리려고 원격 API를 하나 더 붙였다. Haiku는 분류·추출 같은
짧은 작업용 저가 모델이라 fast·quality 둘 다 Haiku로 두고, 생각(thinking)은 꺼서 지연을 줄인다.

OpenAI 백엔드와 다른 점
  - 구조화 출력: OpenAI JSON 모드는 '유효한 JSON'만 보장했지만, Claude는
    output_config.format(json_schema)로 스키마 자체를 강제한다. 그래도 내용(시스템 이름 오타,
    task 누락)은 흔들릴 수 있어서 plan 보정 코드는 그대로 둔다.
  - 토큰 스트리밍: chat·write는 messages.stream()으로 받아 on_token을 조각마다 부른다
    (화면에 답변이 실시간으로 찍힌다).
  - 비용: 입력·출력 토큰을 실제 단가로 따로 곱한다 (Haiku 5.5: $0.10 / $0.50 per MTok).

키는 .env의 ANTHROPIC_API_KEY에서 SDK가 직접 읽는다. 코드·로그·화면 어디에도 찍지 않는다.
"""
import json
import os
import re

import anthropic
from langsmith import traceable

from .openai_llm import KNOWN_SYSTEMS, _guess_target, _guess_targets

DEFAULT_MODEL = "claude-haiku-5-5"
# Haiku 5.5 실제 단가 (프롬프트 100K 토큰 이하 구간), USD per 1M tokens
PRICE_PER_MTOK = {"claude-haiku-5-5": (0.10, 0.50)}

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["chat", "incident"]},
        "needs_analysis": {"type": "boolean"},
    },
    "required": ["route", "needs_analysis"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_analysis": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": ["research", "analysis"]},
                    "target": {"type": "string"},
                },
                "required": ["agent", "target"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["needs_analysis", "tasks"],
    "additionalProperties": False,
}


def _text_of(message) -> str:
    """응답에서 text 블록만 이어 붙인다 (thinking을 켜도 안전하도록)."""
    return "".join(b.text for b in message.content if b.type == "text")


class AnthropicLLM:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str | None = None,
                 thinking: bool | None = None):
        self.name = f"anthropic:{model}"
        self.model = model
        self.price_in, self.price_out = PRICE_PER_MTOK.get(model, (0.10, 0.50))
        self.effort = effort or os.environ.get("ANTHROPIC_EFFORT", "low")
        if thinking is None:
            thinking = os.environ.get("ANTHROPIC_THINKING", "off") == "on"
        self.thinking = thinking
        # 키 확인은 첫 호출 때 한다 — fake/ollama로 띄운 서버는 이 모듈을 import하지 않는다
        self._client: anthropic.Anthropic | None = None
        self._aclient: anthropic.AsyncAnthropic | None = None

    def _check_key(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY가 설정되지 않았습니다 — incident_rag_agent/.env에 넣으세요."
            )

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._check_key()
            self._client = anthropic.Anthropic(max_retries=2, timeout=60.0)
        return self._client

    @property
    def aclient(self) -> anthropic.AsyncAnthropic:
        if self._aclient is None:
            self._check_key()
            self._aclient = anthropic.AsyncAnthropic(max_retries=2, timeout=120.0)
        return self._aclient

    def _params(self, system: str, user: str, max_tokens: int,
                schema: dict | None = None) -> dict:
        output_config: dict = {"effort": self.effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        params = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        # Haiku 5.5는 기본이 adaptive thinking — 짧은 분류·요약에는 지연만 늘어서 끈다.
        # (disabled는 effort high 이하에서만 허용된다)
        if not self.thinking:
            params["thinking"] = {"type": "disabled"}
        return params

    def _usage(self, message) -> dict:
        u = message.usage
        tokens = (u.input_tokens or 0) + (u.output_tokens or 0)
        usd = ((u.input_tokens or 0) * self.price_in
               + (u.output_tokens or 0) * self.price_out) / 1_000_000
        return {"model": self.name, "tokens": tokens, "usd": round(usd, 6),
                "stop_reason": message.stop_reason}

    def _chat(self, system: str, user: str, schema: dict | None = None,
              max_tokens: int = 1024) -> tuple[str, dict]:
        message = self.client.messages.create(**self._params(system, user, max_tokens, schema))
        # refusal이면 text가 비거나 잘린다 — 호출부의 코드 보정(파싱 실패 처리)이 받는다
        return _text_of(message), self._usage(message)

    async def _chat_stream(self, system: str, user: str, on_token=None,
                           max_tokens: int = 2048) -> tuple[str, dict]:
        chunks: list[str] = []
        async with self.aclient.messages.stream(
                **self._params(system, user, max_tokens)) as stream:
            async for text in stream.text_stream:
                chunks.append(text)
                if on_token is not None:
                    on_token(text)
            message = await stream.get_final_message()
        usage = self._usage(message)
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        return "".join(chunks), usage


class AnthropicFastLLM(AnthropicLLM):
    """분류·계획·패턴 분석·단순 대화."""

    @traceable(run_type="llm", name="anthropic_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 지원 라우터다. 사용자 요청을 분류하라. "
                  "장애·성능 저하·지연·진단·모니터링·티켓 등 운영 이슈면 route=incident, "
                  "인사·잡담·일반 질문이면 route=chat. "
                  "needs_analysis는 패턴 비교/통계 분석이 꼭 필요할 때만 true.")
        content, usage = self._chat(system, query, schema=ROUTE_SCHEMA, max_tokens=200)
        try:
            raw = json.loads(content)
            route = raw.get("route") if raw.get("route") in ("chat", "incident") else None
            result = {"route": route or "chat",
                      "needs_analysis": bool(raw.get("needs_analysis", False))}
        except (json.JSONDecodeError, AttributeError):
            incident = bool(re.search(r"장애|진단|다운|지연|급증|incident|티켓", query))
            result = {"route": "incident" if incident else "chat", "needs_analysis": False}
        return result, usage

    @traceable(run_type="llm", name="anthropic_fast.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 Supervisor의 planning 노드다. 표준 진단 절차에 따라 "
                  "실행 계획(tasks)을 세워라. "
                  "사용자 요청에 등장한 시스템마다 agent=research task를 하나씩 세워라 "
                  f"(알려진 시스템: {', '.join(sorted(KNOWN_SYSTEMS))} — 특정 시스템이 "
                  "언급되지 않았으면 auth-service를 기본으로 조사하라). "
                  "target에는 시스템 이름을 그대로 쓰고, agent=analysis면 target은 빈 문자열로 둔다. "
                  "패턴 분석이 꼭 필요하다고 판단되면 agent=analysis task를 추가해도 된다.")
        user = f"## 표준 절차\n{procedure_prompt}\n\n## 사용자 요청\n{query}"
        content, usage = self._chat(system, user, schema=PLAN_SCHEMA, max_tokens=1000)
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raw = {"needs_analysis": False, "tasks": []}
        return _normalize_plan(raw, query), usage

    @traceable(run_type="llm", name="anthropic_fast.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 패턴 분석가다. 여러 시스템의 모니터링 결과를 비교해 "
                  "공통 원인이 의심되는 패턴이 있는지 한두 문장으로 짧게 답하라. "
                  "패턴이 없으면 '특이 패턴 없음'이라고만 답하라.")
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        facts = "\n".join(
            f"- {d['system_name']}: 상태={d['status']}, CPU {d['cpu_percent']}%, "
            f"알림 {d['alerts']}" for d in mon) or "(모니터링 데이터 없음)"
        return self._chat(system, facts, max_tokens=300)

    @traceable(run_type="llm", name="anthropic_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        system = ("당신은 간결한 한국어 비서다. 한두 문장으로 답하라. "
                  "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라.")
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        return await self._chat_stream(system, user, on_token=on_token, max_tokens=500)


class AnthropicQualityLLM(AnthropicLLM):
    """최종 답변 작성."""

    @traceable(run_type="llm", name="anthropic_quality.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
                    feedback: str | None, on_token=None) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 보고 작성자다. 수집된 사실만 사용해 한국어로 "
                  "간결한 대응 보고를 작성하라. 반드시 '1) ... 2) ... 3) ...' 형식의 "
                  "번호 절차로 답하고, 수치는 수집된 값을 그대로 인용하라. "
                  "runbook마다 적용 시스템이 적혀 있으니 시스템별로 맞는 절차를 구분해 제시하라.")
        facts = []
        for c in collected:
            if c["source"] == "monitoring":
                d = c["data"]
                facts.append(f"- 모니터링 {d['system_name']}: 상태={d['status']}, "
                             f"CPU {d['cpu_percent']}%, 메모리 {d['memory_percent']}%, "
                             f"알림 {len(d['alerts'])}건 {d['alerts']}")
            elif c["source"] == "runbook":
                systems = ", ".join(c.get("systems", [])) or "조사 대상 시스템"
                facts.append(f"- runbook 발췌 (적용 시스템: {systems}):\n{c['data'].strip()}")
            elif c["source"] == "analysis":
                facts.append(f"- 패턴 분석: {c['data'].strip()}")
        user = f"## 사용자 요청\n{query}\n\n## 수집된 사실\n" + "\n".join(facts)
        if feedback:
            user += f"\n\n## 이전 초안에 대한 검증 반려 사유 (반드시 반영)\n{feedback}"
        return await self._chat_stream(system, user, on_token=on_token, max_tokens=2048)


def _normalize_plan(raw: dict, query: str) -> dict:
    """openai_llm.plan과 같은 보정 — 스키마는 강제돼도 시스템 이름 오타·누락은 남는다."""
    tasks, seen, idx = [], set(), 0
    analysis_added = False
    for t in raw.get("tasks", []):
        agent = t.get("agent")
        if agent == "research":
            target = t.get("target", "")
            target = target if target in KNOWN_SYSTEMS else _guess_target(query)
            if target in seen:
                continue
            seen.add(target)
            tasks.append({"id": f"task-{idx}", "agent": "research",
                          "kind": "monitoring", "target": target, "status": "pending"})
            idx += 1
        elif agent == "analysis" and not analysis_added:
            analysis_added = True
            tasks.append({"id": "task-analysis", "agent": "analysis",
                          "kind": "pattern-analysis", "status": "pending"})
    # 쿼리에 이름이 그대로 나온 시스템은 모델이 빠뜨렸어도 조사한다 (계획 보정)
    missing = [s for s in _guess_targets(query) if s not in seen]
    recovered = [{"id": f"task-{idx + i}", "agent": "research", "kind": "monitoring",
                  "target": target, "status": "pending"} for i, target in enumerate(missing)]
    tasks = recovered + tasks if not seen else tasks + recovered
    return {"needs_analysis": bool(raw.get("needs_analysis", False)), "tasks": tasks}
