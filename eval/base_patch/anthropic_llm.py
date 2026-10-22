"""[평가 전용] Base 코드 + Claude 전송 계층만 교체.

Base(all_total_vue_student)의 OpenAI 백엔드를 그대로 상속해 프롬프트·plan 보정·write 형식은
Base 그대로 두고, _chat만 Claude Haiku 5.5 호출로 바꾼다. 개선 후 버전과 같은 모델·같은
구조화 출력(json_schema)·같은 thinking/effort 설정으로 맞춰, 차이가 코드에서만 나오게 한다.
"""
import os

import anthropic
from langsmith import traceable

from .openai_llm import OpenAIFastLLM, OpenAIQualityLLM

MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10, 0.50
ROUTE_SCHEMA = {"type": "object", "properties": {
    "route": {"type": "string", "enum": ["chat", "incident"]},
    "needs_analysis": {"type": "boolean"}},
    "required": ["route", "needs_analysis"], "additionalProperties": False}
PLAN_SCHEMA = {"type": "object", "properties": {
    "needs_analysis": {"type": "boolean"},
    "tasks": {"type": "array", "items": {"type": "object", "properties": {
        "agent": {"type": "string", "enum": ["research", "analysis"]},
        "target": {"type": "string"}},
        "required": ["agent", "target"], "additionalProperties": False}}},
    "required": ["needs_analysis", "tasks"], "additionalProperties": False}


class _ClaudeTransport:
    def __init__(self, model: str = MODEL, usd_per_1k_tokens: float = 0.0):
        self.name = f"anthropic:{model}"
        self.model = model
        self.client = anthropic.Anthropic(max_retries=2, timeout=120.0)

    def _chat(self, system: str, user: str, json_mode: bool = False):
        params = {"model": self.model, "max_tokens": 2048, "system": system,
                  "messages": [{"role": "user", "content": user}],
                  "thinking": {"type": "disabled"},
                  "output_config": {"effort": os.environ.get("ANTHROPIC_EFFORT", "low")}}
        if json_mode:
            schema = PLAN_SCHEMA if "planning" in system else ROUTE_SCHEMA
            params["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        m = self.client.messages.create(**params)
        text = "".join(b.text for b in m.content if b.type == "text")
        u = m.usage
        usd = (u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT) / 1_000_000
        return text, {"model": self.name, "tokens": u.input_tokens + u.output_tokens,
                      "usd": round(usd, 6)}


# Base 메서드 본문(__wrapped__)을 그대로 부르되 LangSmith 기록 이름만 anthropic_*로 바꾼다
class AnthropicFastLLM(_ClaudeTransport, OpenAIFastLLM):
    @traceable(run_type="llm", name="anthropic_fast.route")
    def route(self, query):
        return OpenAIFastLLM.route.__wrapped__(self, query)

    @traceable(run_type="llm", name="anthropic_fast.plan")
    def plan(self, query, procedure_prompt):
        return OpenAIFastLLM.plan.__wrapped__(self, query, procedure_prompt)

    @traceable(run_type="llm", name="anthropic_fast.analyze")
    def analyze(self, collected):
        return OpenAIFastLLM.analyze.__wrapped__(self, collected)

    @traceable(run_type="llm", name="anthropic_fast.chat")
    async def chat(self, query, history, on_token=None):
        return await OpenAIFastLLM.chat.__wrapped__(self, query, history, on_token)


class AnthropicQualityLLM(_ClaudeTransport, OpenAIQualityLLM):
    @traceable(run_type="llm", name="anthropic_quality.write")
    async def write(self, query, collected, attempt, feedback, on_token=None):
        return await OpenAIQualityLLM.write.__wrapped__(self, query, collected, attempt,
                                                        feedback, on_token)
