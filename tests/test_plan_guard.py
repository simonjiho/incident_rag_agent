"""plan() 보정 — 소형 모델이 시스템명을 틀리거나 빠뜨려도 쿼리에 나온 시스템은 모두 조사한다.

실모델(qwen3:0.6b) e2e에서 3개 시스템 질의 중 2개가 조사에서 통째로 빠진 사례를 재현한다.
_chat만 바꿔 끼우므로 Ollama·OpenAI 호출은 없다.
"""
import json

import pytest

from agent.ollama_llm import OllamaFastLLM
from agent.openai_llm import OpenAIFastLLM

QUERY = "ledger-service, recommend-service, gateway-service 세 곳에서 동시에 장애가 났어요. 진단해줘."


def _fake_chat(tasks: list[dict]):
    def chat(system, user, schema=None, json_mode=False):   # ollama: schema / openai: json_mode
        return json.dumps({"needs_analysis": False, "tasks": tasks}), {"usd": 0.0, "tokens": 0}
    return chat


def _make(backend, monkeypatch):
    if backend == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
        return OpenAIFastLLM("gpt-4o-mini", usd_per_1k_tokens=0.00015)
    return OllamaFastLLM("qwen3:0.6b", usd_per_1k_tokens=0.00015)


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_misspelled_systems_are_recovered(backend, monkeypatch):
    llm = _make(backend, monkeypatch)
    # 모델이 첫 시스템만 제대로 쓰고 나머지 이름을 틀림 → 예전 코드는 둘 다 ledger-service로 바뀌어 버려졌다
    monkeypatch.setattr(llm, "_chat", _fake_chat([
        {"agent": "research", "target": "ledger-service"},
        {"agent": "research", "target": "recommend service"},
        {"agent": "research", "target": "gateway"},
    ]))
    result, _ = llm.plan(QUERY, "절차")
    targets = [t["target"] for t in result["tasks"] if t["agent"] == "research"]
    assert sorted(targets) == ["gateway-service", "ledger-service", "recommend-service"]
    ids = [t["id"] for t in result["tasks"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_empty_plan_recovers_all_named_systems(backend, monkeypatch):
    llm = _make(backend, monkeypatch)
    monkeypatch.setattr(llm, "_chat", _fake_chat([]))
    result, _ = llm.plan(QUERY, "절차")
    targets = [t["target"] for t in result["tasks"] if t["agent"] == "research"]
    assert sorted(targets) == ["gateway-service", "ledger-service", "recommend-service"]


def test_correct_plan_is_unchanged(monkeypatch):
    llm = _make("ollama", monkeypatch)
    monkeypatch.setattr(llm, "_chat", _fake_chat([
        {"agent": "research", "target": "gateway-service"},
        {"agent": "research", "target": "ledger-service"},
        {"agent": "research", "target": "recommend-service"},
    ]))
    result, _ = llm.plan(QUERY, "절차")
    assert [t["target"] for t in result["tasks"]] == ["gateway-service", "ledger-service",
                                                     "recommend-service"]
