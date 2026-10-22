# 개선 전 코드를 Claude로 돌리기 (평가 전용)

원본 Base(`all_total_vue_student`)에는 Claude 백엔드가 없다. 같은 모델로 Before/After를 재려고
전송 계층만 바꾼 어댑터를 둔다. `anthropic_llm.py`는 Base의 `OpenAIFastLLM`·`OpenAIQualityLLM`을
그대로 상속해 프롬프트·plan 보정·write 형식은 Base 그대로 두고 `_chat`만 Claude 호출로 바꾼다
(개선 후 버전과 같은 json_schema 구조화 출력, thinking 끔, effort low).

1. Base를 다른 곳에 복사하고 `data/`를 이 저장소 것으로 덮는다 (`doc/IMPROVEMENTS.md` §5).
2. 이 폴더의 `anthropic_llm.py`를 복사본의 `agent/`에 넣는다.
3. 복사본 `server.py`의 `_build_llms()` 맨 앞에 추가한다:

```python
    if backend == "anthropic":   # [평가 전용] Base + Claude 전송 계층
        from agent.anthropic_llm import AnthropicFastLLM, AnthropicQualityLLM
        return backend, AnthropicFastLLM(), AnthropicQualityLLM()
```

4. 키는 이 저장소의 `.env`를 셸에서 source해 넘기고(`set -a; source .env; set +a`),
   `LLM_BACKEND=anthropic`으로 복사본 서버를 띄운 뒤 `eval/run_eval.py e2e`를 돌린다.
