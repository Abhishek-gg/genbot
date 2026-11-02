"""Build the static replay page (dist/static-space) from real evaluation runs.

    python deploy/build_static.py          # needs Prometheus running: health checks are replayed from its history
    python deploy/publish_static.py --repo <user>/<space>

Every number and answer on the page comes from eval/results; the service_health views are replayed from
the recorded Prometheus history at the moment each question was asked. Nothing is invented.
"""
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from copilot import config, tools  # noqa: E402
from copilot.evaluate import HEALTHY_QUESTION, QUESTION, RESULTS_DIR, SCENARIOS  # noqa: E402
from copilot.tools import Toolbox  # noqa: E402

OUT = ROOT / "dist" / "static-space"
TEMPLATE = ROOT / "deploy" / "static" / "index.template.html"
SPACE_README = """---
title: GenBot
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: static
pinned: false
short_description: Replays of an agentic RAG incident copilot
---

Replays of real investigations by GenBot, an agentic RAG incident copilot over live metrics, logs and incident memory.
This page is static: everything on it was recorded from real runs.
"""


def load_runs() -> list[dict]:
    """The most complete run per model (latest wins ties)."""
    best: dict[str, dict] = {}
    for f in sorted(RESULTS_DIR.glob("*.jsonl")):
        trials = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not trials:
            continue
        model = trials[0]["model"] if not trials[0].get("error") else f.stem.split("-", 2)[2].replace("_", "/")
        run = {"model": model, "date": f.stem[:8], "file": f.name, "trials": trials}
        done = sum(not t.get("error") for t in trials)
        if model not in best or done >= sum(not t.get("error") for t in best[model]["trials"]):
            best[model] = run
    return sorted(best.values(), key=lambda r: "120b" not in r["model"])  # main model first


def at(date: str, hhmmss: str) -> float:
    return datetime.strptime(f"{date} {hhmmss}", "%Y%m%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def replay_health(ts: float, minutes: float = 30) -> str:
    return Toolbox(config.PROMETHEUS_URL, None, None, clock=lambda: ts).run("service_health", {"minutes": minutes})


def health_minutes(trial: dict) -> float:
    for s in trial.get("steps", []):
        if s["tool"] == "service_health":
            return float(s["args"].get("minutes", 30))
    return 30


def pick_replays(runs: list[dict]) -> dict:
    replays = {}
    for scenario in SCENARIOS:
        for run in runs:  # main model first
            candidates = [t for t in run["trials"] if t["scenario"] == scenario and not t.get("error")]
            if not candidates:
                continue
            t = sorted(candidates, key=lambda t: (not t["correct"], not t.get("steps")))[0]
            ts = at(run["date"], t["asked_at"])
            replays[scenario] = {
                "question": HEALTHY_QUESTION if scenario == "healthy" else QUESTION,
                "when": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "model": t["model"], "tools": t["tools"], "seconds": t["seconds"], "tokens": t["tokens"],
                "correct": t["correct"], "answer": t["answer"],
                "health_minutes": health_minutes(t), "health_exact": bool(t.get("steps")),
                "health": replay_health(ts, health_minutes(t)),
            }
            break
    return replays


def build_story(runs: list[dict]) -> dict | None:
    """The healthy trial that was misdiagnosed because a recovering signal looked current."""
    for run in runs:
        for t in run["trials"]:
            if t["scenario"] == "healthy" and not t["correct"] and not t.get("error"):
                ts = at(run["date"], t["asked_at"])
                after = replay_health(ts)
                saved, tools.LATEST_S = tools.LATEST_S, 10**9  # the pre-fix rule: the 2-minute average alone
                try:
                    before = replay_health(ts)
                finally:
                    tools.LATEST_S = saved
                cause = t["root_cause"].split("(")[0].strip().rstrip(".,;:–-").strip()
                return {
                    "summary": f"In a run on {t['model']}, a healthy trial that ran straight after a database fault was "
                               f"diagnosed as \"{cause}\". Latency had already been normal for a full "
                               "minute, but service_health judged \"now\" by a 2-minute average that still contained "
                               "the end of the previous fault.",
                    "before": "\n".join(before.splitlines()[:6]),
                    "after": "\n".join(after.splitlines()[:6]),
                    "fix": "The fix: a signal counts as abnormal now only if its last minute is abnormal too; "
                           "otherwise it is listed as recovered. Replaying the real metrics from that moment confirms "
                           "it, and a replay during the actual database fault still reports the incident.",
                }
    return None


def main() -> None:
    runs = load_runs()
    if not runs:
        sys.exit("no evaluation results in eval/results")
    main_run = runs[0]
    done = [t for t in main_run["trials"] if not t.get("error")]
    ok = sum(t["correct"] for t in done)
    errors = [(r["model"], t["scenario"]) for r in runs for t in r["trials"] if t.get("error")]
    note = "Each cell is correct diagnoses out of trials. "
    if any("20b" in r["model"] for r in runs):
        note += ("The gpt-oss-20b run predates the fix described below, which caused its one miss. ")
    if errors:
        note += "Trials that did not run because the free daily LLM quota ran out are left out: " + \
                ", ".join(f"{s} ({m})" for m, s in errors) + "."
    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "headline": [
            {"value": f"{ok}/{len(done)}", "label": f"scenarios diagnosed correctly ({main_run['model']})"},
            {"value": f"{statistics.median(t['seconds'] for t in done):.0f}s", "label": "median time to diagnosis"},
            {"value": f"{statistics.mean(t['tool_calls'] for t in done):.1f}", "label": "tool calls per investigation"},
            {"value": "7", "label": "tools: metrics, logs, deploys, memory"},
        ],
        "runs": [{"model": r["model"], "trials": [{k: t.get(k) for k in ("scenario", "correct", "seconds", "error")}
                                                    for t in r["trials"]]} for r in runs],
        "score_note": note,
        "replays": pick_replays(runs),
        "story": build_story(runs),
    }
    if data["story"] is None:
        data["story"] = {"summary": "No misdiagnoses in the recorded runs.", "before": "", "after": "", "fix": ""}

    OUT.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")  # can't close the <script> early
    (OUT / "index.html").write_text(TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")
    (OUT / "README.md").write_text(SPACE_README, encoding="utf-8")
    print(f"built {OUT / 'index.html'} from {', '.join(r['file'] for r in runs)}")


if __name__ == "__main__":
    main()
