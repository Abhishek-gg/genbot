import json

import httpx
import pytest

from copilot.llm import ChatClient, LLMError


def client(handler, monkeypatch, sleeps=None):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    return ChatClient("groq", http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleep=(sleeps.append if sleeps is not None else lambda s: None))


def ok(message, usage=(10, 5)):
    return httpx.Response(200, json={"choices": [{"message": message}],
                                     "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})


def test_sends_tools_and_strips_provider_extras(monkeypatch):
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return ok({"role": "assistant", "content": None, "reasoning": "hidden",
                   "tool_calls": [{"id": "c1", "type": "function", "index": 0,
                                   "function": {"name": "service_health", "arguments": '{"minutes": 5}'}}]})

    c = client(handler, monkeypatch)
    msg = c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x"}}])

    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "openai/gpt-oss-120b" and seen["body"]["tool_choice"] == "auto"
    assert msg == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "service_health", "arguments": '{"minutes": 5}'}}]}
    assert c.usage == {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5}


def test_retries_rate_limit_using_retry_after(monkeypatch):
    responses = iter([httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(503),
                      ok({"role": "assistant", "content": "done"})])
    sleeps = []
    c = client(lambda req: next(responses), monkeypatch, sleeps)
    assert c.chat([{"role": "user", "content": "hi"}])["content"] == "done"
    assert sleeps == [7.5, 4]


def test_non_retryable_error_raises(monkeypatch):
    c = client(lambda req: httpx.Response(401, text="bad key"), monkeypatch)
    with pytest.raises(LLMError, match="401"):
        c.chat([{"role": "user", "content": "hi"}])


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GROQ_API_KEY is not set"):
        ChatClient("groq")


def test_retries_groq_output_parse_failure(monkeypatch):
    responses = iter([httpx.Response(400, json={"error": {"code": "output_parse_failed"}}),
                      ok({"role": "assistant", "content": "done"})])
    c = client(lambda req: next(responses), monkeypatch)
    assert c.chat([{"role": "user", "content": "hi"}])["content"] == "done"
