"""Evaluation: inject known faults, let GenBot diagnose them, and score the answers.

    python -m copilot.evaluate --trials 2
    python -m copilot.evaluate --trials 3 --model openai/gpt-oss-20b --scenarios cpu,database

Needs the Docker stack and `python -m copilot.ingest` running, and the alert responder
stopped (it would investigate the same faults and spend the token quota twice).
Each trial: reset all faults, let the service settle, inject one fault, wait for symptoms,
ask a neutral question, then grade the **Root cause** line against the injected fault.
Results go to eval/results/<timestamp>-<model>.jsonl and a Markdown report next to it.
"""
import argparse
import json
import random
import re
import statistics
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .llm import LLMError, PROVIDERS
from .logstore import LogStore
from .memory import IncidentMemory

SHOPAPI = "http://localhost:8000"
RESULTS_DIR = config.ROOT / "eval" / "results"
QUESTION = "Something is wrong with ShopAPI right now. What's causing it?"
HEALTHY_QUESTION = "Is anything wrong with ShopAPI right now?"

# name -> (chaos settings, expected category)
SCENARIOS = {
    "healthy": (None, "healthy"),
    "database": ({"db_latency_s": 1.5}, "database"),
    "payment": ({"error_rate": 0.5}, "payment"),
    "memory": ({"memory_leak_kb": 8192}, "memory"),
    "cpu": ({"cpu_burn_ms": 250}, "cpu"),
}

# Keywords that identify each cause. The category whose keyword appears *earliest* in the root-cause
# line wins, so "memory leak leading to CPU saturation" is graded as memory, not CPU.
CATEGORIES = {
    "payment": ["payment", "gateway"],
    "database": ["database", "db query", "db slow", "db latency", "slow quer", "query latency"],
    "memory": ["memory", "leak", "cache growth", "order cache", "order-cache"],
    "cpu": ["cpu", "event loop", "event-loop", "process saturation"],
}
HEALTHY_WORDS = ["none", "n/a", "no current", "healthy", "no issue", "nothing"]


def root_cause_line(answer: str) -> str:
    m = re.search(r"\*\*Root cause\*\*\s*:?\s*(.+)", answer, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def grade(answer: str) -> str:
    """Return the cause category the answer commits to: a CATEGORIES key, 'healthy' or 'unknown'."""
    line = root_cause_line(answer)
    if not line:
        return "unknown"
    text = line.lower().replace("‑", "-").replace("‐", "-")
    # "none (an earlier payment issue has recovered)" is a healthy verdict that merely mentions a cause.
    if text.lstrip("*_ ").startswith(tuple(HEALTHY_WORDS)):
        return "healthy"
    hits = [(text.find(k), cat) for cat, keys in CATEGORIES.items() for k in keys if k in text]
    if hits:
        return min(hits)[1]
    return "healthy" if any(w in text for w in HEALTHY_WORDS) else "unknown"


@dataclass
class Trial:
    scenario: str
    expected: str
    predicted: str
    correct: bool
    model: str
    seconds: float
    tool_calls: int
    llm_calls: int
    tokens: int
    tools: list[str]
    root_cause: str
    answer: str
    error: str | None = None
    asked_at: str = ""
    steps: list[dict] = field(default_factory=list)  # each tool call with its arguments and (truncated) output


def set_chaos(settings: dict | None) -> None:
    method, body = ("DELETE", None) if settings is None else ("POST", json.dumps(settings).encode())
    req = urllib.request.Request(f"{SHOPAPI}/chaos", data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def preflight() -> str | None:
    """Return a reason the evaluation can't run, or None."""
    try:
        urllib.request.urlopen(f"{SHOPAPI}/health", timeout=5).read()
    except OSError:
        return "ShopAPI is not reachable; start the stack with `docker compose up -d`."
    try:
        urllib.request.urlopen(f"http://localhost:{config.RESPONDER_PORT}/health", timeout=2).read()
        return "The alert responder is running; stop it first, or it will investigate the same faults."
    except OSError:
        pass
    last = LogStore(config.LOG_DB).max_ts()
    if last is None or time.time() - last > 60:
        return "Logs are not being ingested; start `python -m copilot.ingest` first."
    return None


def run_trial(name: str, model: str, memory: IncidentMemory, settle_s: int, fault_s: int) -> Trial:
    from .ask import build_agent  # imported late so `--help` works without the stack

    settings, expected = SCENARIOS[name]
    set_chaos(None)
    time.sleep(settle_s)
    if settings:
        set_chaos(settings)
        time.sleep(fault_s)
    memory.clear()
    asked_at = datetime.now(timezone.utc).strftime("%H:%M:%S")
    agent = build_agent(config.LLM_PROVIDER, model, memory=memory, fallbacks=[])
    try:
        r = agent.ask(QUESTION if settings else HEALTHY_QUESTION)
    except LLMError as e:
        error = re.sub(r"org_[A-Za-z0-9]+", "org_REDACTED", str(e))[:300]  # results are committed; keep account IDs out
        return Trial(name, expected, "error", False, model, 0, 0, 0, 0, [], "", "", error, asked_at)
    finally:
        set_chaos(None)
    predicted = grade(r.answer)
    return Trial(name, expected, predicted, predicted == expected, r.model, round(r.seconds, 1), len(r.steps),
                 r.llm_calls, r.tokens, [s.tool for s in r.steps], root_cause_line(r.answer), r.answer,
                 asked_at=asked_at,
                 steps=[{"tool": s.tool, "args": s.args, "result": s.result[:2000]} for s in r.steps])


def report(trials: list[Trial], model: str, started: str) -> str:
    done = [t for t in trials if not t.error]
    lines = [f"# GenBot evaluation: {model}", "",
             f"Run started {started} UTC. {len(trials)} trials, {len(trials) - len(done)} errors. "
             "Each trial injects one fault into a clean service, waits for symptoms, asks a neutral question "
             "and grades the **Root cause** line.", "",
             "| Scenario | Correct | Accuracy | Median time | Median tool calls |", "|---|---|---|---|---|"]
    for name in SCENARIOS:
        ts = [t for t in done if t.scenario == name]
        if ts:
            ok = sum(t.correct for t in ts)
            lines.append(f"| {name} | {ok}/{len(ts)} | {ok / len(ts):.0%} | {statistics.median(t.seconds for t in ts):.0f}s "
                         f"| {statistics.median(t.tool_calls for t in ts):g} |")
    if done:
        ok = sum(t.correct for t in done)
        secs = sorted(t.seconds for t in done)
        lines += ["", f"**Overall: {ok}/{len(done)} correct ({ok / len(done):.0%}).** "
                      f"Time to diagnosis after the question: median {statistics.median(secs):.0f}s, "
                      f"slowest {secs[-1]:.0f}s. Mean tool calls {statistics.mean(t.tool_calls for t in done):.1f}, "
                      f"mean tokens {statistics.mean(t.tokens for t in done):,.0f}. "
                      f"Saved a postmortem in {sum('save_postmortem' in t.tools for t in done if t.expected != 'healthy')}"
                      f"/{sum(t.expected != 'healthy' for t in done)} fault trials."]
    wrong = [t for t in done if not t.correct]
    if wrong:
        lines += ["", "## Misdiagnoses", ""]
        lines += [f"- **{t.scenario}** graded as *{t.predicted}*: {t.root_cause}" for t in wrong]
    errors = [t for t in trials if t.error]
    if errors:
        lines += ["", "## Errors", ""] + [f"- {t.scenario}: {t.error}" for t in errors]
    return "\n".join(lines) + "\n"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=2, help="trials per scenario")
    ap.add_argument("--model", default=config.LLM_MODEL or PROVIDERS[config.LLM_PROVIDER][2])
    ap.add_argument("--scenarios", default=",".join(SCENARIOS), help="comma-separated subset")
    # Must exceed service_health's 2-minute "now" window plus the 1-minute rate windows, or the previous
    # trial's fault still looks current (this caused a misdiagnosis with a 120 s settle).
    ap.add_argument("--settle", type=int, default=210, help="seconds of normal traffic before each fault")
    ap.add_argument("--fault", type=int, default=120, help="seconds a fault runs before the question")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    names = [n.strip() for n in args.scenarios.split(",")]
    if unknown := [n for n in names if n not in SCENARIOS]:
        sys.exit(f"unknown scenarios: {unknown}; choose from {list(SCENARIOS)}")
    if reason := preflight():
        sys.exit(reason)

    # Shuffle within each round so no scenario always follows the same one.
    rng = random.Random(args.seed)
    order = []
    for _ in range(args.trials):
        rnd = names[:]
        rng.shuffle(rnd)
        order += rnd

    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    stem = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M") + "-" + args.model.replace("/", "_")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    jsonl, md = RESULTS_DIR / f"{stem}.jsonl", RESULTS_DIR / f"{stem}.md"
    memory = IncidentMemory(config.DATA_DIR / "eval_incidents.db", config.CHROMA, collection="incidents_eval")
    per_trial = args.settle + args.fault + 60
    print(f"[eval] {len(order)} trials on {args.model}, about {len(order) * per_trial / 60:.0f} min", flush=True)

    trials: list[Trial] = []
    try:
        for i, name in enumerate(order, 1):
            t = run_trial(name, args.model, memory, args.settle, args.fault)
            trials.append(t)
            with open(jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(t)) + "\n")
            verdict = "ERROR" if t.error else ("ok" if t.correct else f"WRONG ({t.predicted})")
            print(f"[eval] {i}/{len(order)} {name}: {verdict}  {t.seconds}s, {t.tool_calls} tools", flush=True)
            if t.error and "quota" in t.error:
                print("[eval] daily quota exhausted; stopping early", flush=True)
                break
    finally:
        set_chaos(None)
        md.write_text(report(trials, args.model, started), encoding="utf-8")
        print(f"[eval] report: {md}", flush=True)


if __name__ == "__main__":
    main()
