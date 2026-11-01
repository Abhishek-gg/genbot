"""A small OpenAI-compatible chat client.

Groq, Google Gemini and Ollama all expose the OpenAI chat-completions API, so one
client covers every free option. Free tiers rate-limit hard: short limits (per minute)
are retried using the server's Retry-After hint, while an exhausted quota (per day) switches
to a fallback model, since free quotas are counted per model.
"""
import os
import time

import httpx

# provider -> (base URL, API key env var, default model, fallback models with their own quotas)
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b", ["openai/gpt-oss-20b"]),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash",
               ["gemini-2.5-flash-lite"]),
    "ollama": ("http://localhost:11434/v1", None, "qwen2.5:7b", []),
}
RETRYABLE = {429, 500, 502, 503, 504}
MAX_WAIT_S = 60


class LLMError(Exception):
    pass


class QuotaExhausted(LLMError):
    """A limit that won't clear within MAX_WAIT_S, such as a daily token quota."""


class ChatClient:
    def __init__(
        self,
        provider: str = "groq",
        model: str | None = None,
        max_retries: int = 5,
        fallbacks: list[str] | None = None,
        http: httpx.Client | None = None,
        sleep=time.sleep,
    ):
        if provider not in PROVIDERS:
            raise LLMError(f"unknown provider {provider!r}; choose from {', '.join(PROVIDERS)}")
        base_url, key_var, default_model, default_fallbacks = PROVIDERS[provider]
        api_key = os.getenv(key_var) if key_var else "ollama"
        if not api_key:
            raise LLMError(f"{key_var} is not set. Add it to .env (see .env.example).")

        self.provider, self.model = provider, model or default_model
        self.fallbacks = [m for m in (default_fallbacks if fallbacks is None else fallbacks) if m != self.model]
        self.max_retries, self.sleep = max_retries, sleep
        self.http = http or httpx.Client(timeout=120)
        self.url = f"{base_url}/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Send the conversation and return the assistant message (content and/or tool_calls).
        If the current model's quota is used up, continue on the next fallback model."""
        while True:
            try:
                return self._chat(messages, tools)
            except QuotaExhausted:
                if not self.fallbacks:
                    raise
                self.model = self.fallbacks.pop(0)

    def _chat(self, messages: list[dict], tools: list[dict] | None) -> dict:
        payload = {"model": self.model, "messages": messages, "temperature": 0.1}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.post(self.url, json=payload, headers=self.headers)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise LLMError(f"could not reach {self.provider}: {e}") from e
                self.sleep(2**attempt)
                continue

            if r.status_code == 429 and self._is_quota(r):
                raise QuotaExhausted(f"{self.provider} {self.model}: quota exhausted: {r.text[:300]}")
            if r.status_code in RETRYABLE and attempt < self.max_retries:
                self.sleep(self._wait_time(r, attempt))
                continue
            # Groq rejects a reply it cannot parse into a tool call; sampling again usually works.
            if r.status_code == 400 and "output_parse_failed" in r.text and attempt < self.max_retries:
                self.sleep(1)
                continue
            if r.status_code != 200:
                raise LLMError(f"{self.provider} returned {r.status_code}: {r.text[:500]}")

            data = r.json()
            self.usage["calls"] += 1
            for k in ("prompt_tokens", "completion_tokens"):
                self.usage[k] += (data.get("usage") or {}).get(k, 0)
            msg = data["choices"][0]["message"]
            # Send back only standard fields; providers reject each other's extras.
            out = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                out["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
                    for c in msg["tool_calls"]
                ]
            return out
        raise LLMError(f"{self.provider}: still failing after {self.max_retries} retries")

    @staticmethod
    def _is_quota(r: httpx.Response) -> bool:
        """Daily quotas (and any limit that won't clear within MAX_WAIT_S) are not worth waiting for."""
        if "per day" in r.text.lower():
            return True
        try:
            return float(r.headers["retry-after"]) > MAX_WAIT_S
        except (KeyError, ValueError):
            return False

    @staticmethod
    def _wait_time(r: httpx.Response, attempt: int) -> float:
        try:
            return min(float(r.headers["retry-after"]) + 0.5, MAX_WAIT_S)
        except (KeyError, ValueError):
            return min(2 ** (attempt + 1), MAX_WAIT_S)
