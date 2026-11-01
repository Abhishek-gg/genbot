"""The incident agent: an LLM in a plan -> tool -> observe loop.

Written by hand rather than with a framework so every step is visible: the model
picks a tool, we run it, feed back the result, and repeat until it answers or
runs out of steps.
"""
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .llm import ChatClient
from .tools import Toolbox, parse_args

MAX_RESULT_CHARS = 3000

SYSTEM_PROMPT = """You are GenBot, an on-call SRE assistant for ShopAPI. Current time: {now} UTC.

ShopAPI is a single-process FastAPI e-commerce service. Routes: GET /products, GET /products/{{product_id}},
POST /orders (calls the external payment-gateway), GET /orders/{{order_id}}. It reads and writes a database
(tables: products, orders). Prometheus metrics: http_requests_total{{method,route,status}},
http_request_duration_seconds{{method,route}} (histogram), db_query_duration_seconds{{table,op}} (histogram),
dependency_errors_total{{dependency}}, order_cache_bytes, shopapi_build_info{{version}},
process_cpu_seconds_total, process_resident_memory_bytes.

How to investigate:
1. Call service_health first. The current problem is what it lists under ABNORMAL NOW. Anything listed as
   recovered is a past incident: never blame the current problem on it. If nothing is abnormal now, the
   service is healthy; say so and mention recovered issues only briefly.
2. Narrow down with search_logs or get_log_lines using the minutes value service_health suggests, so you
   only see this incident's logs, and query_metrics for specifics. Some problems (like CPU saturation)
   leave no log messages at all: missing log evidence is not a reason to blame something else.
3. If the problem started suddenly, check get_recent_deploys.
4. Separate cause from symptoms. Ask: which signal became abnormal first, and which routes are affected?
   A dependency failure shows up on the routes that use it. A slow database shows up as slow queries.
   Saturation of the single process slows every route at once, without slow queries. Steady memory
   growth suggests a leak.
5. Stop once the evidence is consistent. Use at most 6 tool calls for the investigation.
{memory}
Never invent numbers, times or log messages: cite only what tools returned.

Answer in this format:
**Summary**: one sentence.
**Root cause**: the cause, with confidence (high, medium or low).
**Evidence**: 2 to 5 bullets with numbers and UTC times.
**Ruled out**: what you checked that is normal.
**Next steps**: 1 to 3 concrete actions.{memory_answer}"""

MEMORY_PROMPT = """
Incident memory:
- Once service_health shows something abnormal now, call search_past_incidents with those symptoms. A past
  incident is a lead, not proof: confirm the same cause in today's data before relying on it. If it matches and
  was RESOLVED, recommend the fix that worked and cite its ID. An OPEN incident's diagnosis is unconfirmed.
- When you have found the cause of a current problem, call save_postmortem once, as your last tool call,
  with concrete symptoms (numbers and routes). Do not save anything if the service is healthy.
"""
MEMORY_ANSWER = "\n**Incident**: the ID you saved (and any matching past incident), or 'none'."

FORCE_ANSWER = "You have used all your tool calls. Give your best answer now from the evidence so far."
MEMORY_STEPS = ("search_past_incidents", "save_postmortem")
MEMORY_NUDGE = ("Before you answer: service_health shows a current incident, but you have not called {missing}. "
                "Call search_past_incidents with the symptoms, then save_postmortem with your diagnosis, "
                "then give your final answer.")


def missing_memory_steps(steps: list["Step"]) -> list[str]:
    """Memory steps the agent skipped although service_health reported a current incident.

    Weaker models often skip them, so the loop enforces this checklist in code instead of
    trusting the prompt alone."""
    if not any(s.tool == "service_health" and "CURRENT INCIDENT" in s.result for s in steps):
        return []
    done = {s.tool for s in steps}
    return [t for t in MEMORY_STEPS if t not in done]


@dataclass
class Step:
    tool: str
    args: dict
    result: str
    seconds: float


@dataclass
class Result:
    answer: str
    steps: list[Step] = field(default_factory=list)
    llm_calls: int = 0
    tokens: int = 0
    seconds: float = 0.0
    model: str = ""  # the model that produced the answer (may be a fallback)


class Agent:
    def __init__(self, llm: ChatClient, toolbox: Toolbox, max_steps: int = 8,
                 on_step: Callable[[Step], None] | None = None):
        self.llm, self.toolbox, self.max_steps, self.on_step = llm, toolbox, max_steps, on_step

    def ask(self, question: str) -> Result:
        start = time.perf_counter()
        calls_before = self.llm.usage["calls"]
        tokens_before = self.llm.usage["prompt_tokens"] + self.llm.usage["completion_tokens"]

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        tools = self.toolbox.specs()
        has_memory = any(t["function"]["name"] == "save_postmortem" for t in tools)
        system = SYSTEM_PROMPT.format(now=now, memory=MEMORY_PROMPT if has_memory else "",
                                      memory_answer=MEMORY_ANSWER if has_memory else "")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        steps: list[Step] = []
        answer = ""
        nudged = False

        while True:
            out_of_steps = len(steps) >= self.max_steps
            if out_of_steps:
                messages.append({"role": "user", "content": FORCE_ANSWER})
            msg = self.llm.chat(messages, None if out_of_steps else tools)
            messages.append(msg)
            if not msg.get("tool_calls"):
                missing = missing_memory_steps(steps) if has_memory and not nudged and not out_of_steps else []
                if missing:  # send it back once to finish the checklist
                    nudged = True
                    messages.append({"role": "user", "content": MEMORY_NUDGE.format(missing=" or ".join(missing))})
                    continue
                answer = msg["content"].strip()
                break
            for call in msg["tool_calls"]:
                steps.append(self._run(call, messages))

        usage = self.llm.usage
        return Result(
            answer=answer,
            steps=steps,
            llm_calls=usage["calls"] - calls_before,
            tokens=usage["prompt_tokens"] + usage["completion_tokens"] - tokens_before,
            seconds=time.perf_counter() - start,
            model=getattr(self.llm, "model", ""),
        )

    def _run(self, call: dict, messages: list[dict]) -> Step:
        name = call["function"]["name"]
        t0 = time.perf_counter()
        try:
            args = parse_args(call["function"]["arguments"])
            result = self.toolbox.run(name, args)
        except json.JSONDecodeError as e:
            args, result = {}, f"error: arguments were not valid JSON: {e}"
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + "\n...(truncated)"
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        step = Step(name, args, result, time.perf_counter() - t0)
        if self.on_step:
            self.on_step(step)
        return step
