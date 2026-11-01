"""The agent loop, driven by a scripted fake LLM so no API key is needed."""
from copilot.agent import FORCE_ANSWER, Agent


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def chat(self, messages, tools=None):
        self.requests.append((list(messages), tools))
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += 100
        return self.replies.pop(0)


class FakeTools:
    def __init__(self):
        self.calls = []

    @staticmethod
    def specs():
        return [{"type": "function", "function": {"name": "service_health"}}]

    def run(self, name, args):
        self.calls.append((name, args))
        return f"{name} ok"


def tool_call(id_, name, arguments):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_runs_tools_until_the_model_answers():
    llm = FakeLLM([
        {"role": "assistant", "content": "", "tool_calls": [
            tool_call("a", "service_health", '{"minutes": 10}'),
            tool_call("b", "search_logs", "{not json"),
        ]},
        {"role": "assistant", "content": "**Summary**: payment gateway is down."},
    ])
    tools = FakeTools()
    seen = []
    result = Agent(llm, tools, on_step=seen.append).ask("Why are orders failing?")

    assert result.answer == "**Summary**: payment gateway is down."
    assert [s.tool for s in result.steps] == ["service_health", "search_logs"]
    assert tools.calls == [("service_health", {"minutes": 10})]  # bad JSON never reaches the tool
    assert result.steps[1].result.startswith("error: arguments were not valid JSON")
    assert seen == result.steps
    assert result.llm_calls == 2 and result.tokens == 200

    # Second request carries both tool results, matched to their call IDs.
    messages, _ = llm.requests[1]
    assert [(m["tool_call_id"], m["content"][:15]) for m in messages if m["role"] == "tool"] == [
        ("a", "service_health "), ("b", "error: argument")]
    assert "Current time:" in messages[0]["content"]


def test_forces_an_answer_after_max_steps():
    loop = {"role": "assistant", "content": "", "tool_calls": [tool_call("x", "service_health", "{}")]}
    llm = FakeLLM([loop, loop, {"role": "assistant", "content": "best guess"}])
    result = Agent(llm, FakeTools(), max_steps=2).ask("?")

    assert result.answer == "best guess" and len(result.steps) == 2
    final_messages, final_tools = llm.requests[-1]
    assert final_tools is None and final_messages[-1]["content"] == FORCE_ANSWER


def test_long_tool_output_is_truncated():
    class Verbose(FakeTools):
        def run(self, name, args):
            return "x" * 10_000

    llm = FakeLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("a", "service_health", "{}")]},
                   {"role": "assistant", "content": "ok"}])
    result = Agent(llm, Verbose()).ask("?")
    assert len(result.steps[0].result) < 3100 and result.steps[0].result.endswith("(truncated)")


def test_memory_instructions_only_when_memory_tools_exist():
    class WithMemory(FakeTools):
        @staticmethod
        def specs():
            return FakeTools.specs() + [{"type": "function", "function": {"name": "save_postmortem"}}]

    done = {"role": "assistant", "content": "ok"}
    plain, with_mem = FakeLLM([done]), FakeLLM([done])
    Agent(plain, FakeTools()).ask("?")
    Agent(with_mem, WithMemory()).ask("?")
    assert "Incident memory" not in plain.requests[0][0][0]["content"]
    assert "call save_postmortem once" in with_mem.requests[0][0][0]["content"]


class MemoryTools(FakeTools):
    def __init__(self, health="CURRENT INCIDENT: began ~09:33:00 UTC"):
        super().__init__()
        self.health = health

    @staticmethod
    def specs():
        return [{"type": "function", "function": {"name": n}}
                for n in ("service_health", "search_past_incidents", "save_postmortem")]

    def run(self, name, args):
        self.calls.append((name, args))
        return self.health if name == "service_health" else f"{name} ok"


def test_skipped_memory_steps_are_enforced_once():
    answer_early = {"role": "assistant", "content": "premature answer"}
    llm = FakeLLM([
        {"role": "assistant", "content": "", "tool_calls": [tool_call("a", "service_health", "{}")]},
        answer_early,  # tries to answer without touching memory
        {"role": "assistant", "content": "", "tool_calls": [tool_call("b", "search_past_incidents", "{}"),
                                                            tool_call("c", "save_postmortem", "{}")]},
        {"role": "assistant", "content": "final, INC-001 saved"},
    ])
    result = Agent(llm, MemoryTools()).ask("?")
    assert result.answer == "final, INC-001 saved"
    assert [s.tool for s in result.steps] == ["service_health", "search_past_incidents", "save_postmortem"]
    nudge = llm.requests[2][0][-1]["content"]
    assert "not called search_past_incidents or save_postmortem" in nudge


def test_no_nudge_when_healthy_or_after_one_reminder():
    llm = FakeLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("a", "service_health", "{}")]},
                   {"role": "assistant", "content": "healthy"}])
    assert Agent(llm, MemoryTools(health="ABNORMAL NOW: nothing")).ask("?").answer == "healthy"

    stubborn = FakeLLM([{"role": "assistant", "content": "", "tool_calls": [tool_call("a", "service_health", "{}")]},
                        {"role": "assistant", "content": "first"}, {"role": "assistant", "content": "second"}])
    assert Agent(stubborn, MemoryTools()).ask("?").answer == "second"  # reminded once, then accepted
