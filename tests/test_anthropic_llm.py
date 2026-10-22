"""Claude 백엔드 — 네트워크 없이 요청 파라미터와 응답 후처리만 본다.

실제 API는 부르지 않는다. 클라이언트 자리에 가짜 객체를 꽂아 messages.stream이 돌려줄
텍스트·stop_reason·usage를 정해 둔다.
"""
from types import SimpleNamespace

import pytest

from agent.anthropic_llm import (REFUSED_TEXT, THINKING_HEADROOM, TRUNCATED_NOTE,
                                 AnthropicFastLLM, AnthropicQualityLLM)


@pytest.mark.parametrize("model,effort,disabled", [
    ("claude-haiku-5-5", "low", True),
    ("claude-haiku-5-5", "high", True),
    ("claude-haiku-5-5", "xhigh", False),     # Haiku도 xhigh·max에서는 disabled를 400으로 거절
    ("claude-sonnet-5-5", "low", False),      # Sonnet·Opus 5.5는 disabled를 받지 않는다
    ("claude-opus-5-5", "low", False),
])
def test_thinking_param_only_where_accepted(model, effort, disabled):
    llm = AnthropicFastLLM(model, effort=effort, thinking=False)
    params = llm._params("sys", "user", max_tokens=200)
    assert ("thinking" in params) is disabled
    assert params["max_tokens"] == (200 if disabled else 200 + THINKING_HEADROOM)
    assert params["output_config"]["effort"] == effort
    assert "temperature" not in params   # Haiku 5.5는 비기본 샘플링 파라미터를 400으로 거절


def test_thinking_on_leaves_room_for_thinking_tokens():
    params = AnthropicFastLLM(thinking=True)._params("s", "u", max_tokens=300)
    assert "thinking" not in params and params["max_tokens"] == 300 + THINKING_HEADROOM


def test_unknown_model_price_warns():
    with pytest.warns(UserWarning):
        llm = AnthropicFastLLM("claude-some-future-model")
    assert llm.price_known is False


class _FakeStream:
    def __init__(self, pieces, stop_reason):
        self.pieces, self.stop_reason = pieces, stop_reason

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for p in self.pieces:
                yield p
        return gen()

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


def _with_stream(llm, pieces, stop_reason):
    llm._aclient = SimpleNamespace(messages=SimpleNamespace(
        stream=lambda **params: _FakeStream(pieces, stop_reason)))
    return llm


async def test_stream_tokens_usage_and_prompt():
    llm = _with_stream(AnthropicQualityLLM(), ["1) 확인 ", "2) 조치"], "end_turn")
    tokens = []
    text, usage = await llm.write("q", [], 1, None, on_token=tokens.append)
    assert text == "1) 확인 2) 조치" and tokens == ["1) 확인 ", "2) 조치"]
    assert usage["tokens"] == 1200
    assert usage["usd"] == round((1000 * 0.10 + 200 * 0.50) / 1_000_000, 6)
    assert usage["prompt"].startswith("[SYSTEM]")


async def test_truncated_answer_is_marked():
    llm = _with_stream(AnthropicQualityLLM(), ["1) 길게 ..."], "max_tokens")
    tokens = []
    text, _ = await llm.write("q", [], 1, None, on_token=tokens.append)
    assert text.endswith(TRUNCATED_NOTE) and tokens[-1] == TRUNCATED_NOTE


async def test_empty_refusal_gets_fixed_text():
    llm = _with_stream(AnthropicFastLLM(), [], "refusal")
    text, usage = await llm.chat("q", [])
    assert text == REFUSED_TEXT and usage["stop_reason"] == "refusal"
