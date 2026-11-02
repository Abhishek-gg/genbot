import json
import threading
import urllib.request

import httpx

from copilot.agent import Result, Step
from copilot.responder import (COOLDOWN_S, Notifier, Responder, build_question, investigation_message,
                               make_server)


def alert(name, status="firing", severity="critical", summary="5xx above 5%"):
    return {"status": status, "labels": {"alertname": name, "severity": severity},
            "annotations": {"summary": summary},
            "startsAt": "2026-11-01T08:47:27.123Z", "endsAt": "2026-11-01T08:55:02Z"}


def payload(*alerts, key="{}:{job=\"shopapi\"}"):
    status = "firing" if any(a["status"] == "firing" for a in alerts) else "resolved"
    return {"status": status, "groupKey": key, "alerts": list(alerts)}


def result(answer="**Summary**: payment gateway down", save="Saved as INC-007 (open until ...)."):
    steps = [Step("service_health", {}, "...", 0.1), Step("save_postmortem", {}, save, 0.0)]
    return Result(answer=answer, steps=steps, llm_calls=3, tokens=900, seconds=12.4)


class Harness:
    def __init__(self):
        self.questions, self.sent = [], []
        self.t = 1000.0
        self.r = Responder(self._investigate, self.sent.append, model="test-model", clock=lambda: self.t)

    def _investigate(self, question):
        self.questions.append(question)
        return result()


def test_build_question_lists_each_alert():
    q = build_question([alert("HighErrorRate"), alert("HighLatencyP95", severity="warning", summary="p95 above 1s")])
    assert "- HighErrorRate (critical): 5xx above 5% (firing since 08:47:27 UTC)" in q
    assert "- HighLatencyP95 (warning): p95 above 1s" in q
    assert q.endswith("Investigate what is wrong right now and why.")


def test_firing_alert_is_investigated_and_posted():
    h = Harness()
    assert h.r.handle(payload(alert("HighErrorRate"))) == "queued"
    h.r.work_once()
    assert len(h.questions) == 1
    embed = h.sent[0]["embeds"][0]
    assert embed["title"] == "🚨 HighErrorRate · INC-007"
    assert embed["description"].startswith("**Summary**")
    assert embed["footer"]["text"] == "2 tool calls · 12s · test-model"


def test_repeats_are_skipped_but_new_alerts_reinvestigate():
    h = Harness()
    h.r.handle(payload(alert("HighErrorRate")))
    assert h.r.handle(payload(alert("HighErrorRate"))) == "skipped: already investigated"
    # A new alert in the same group is new information.
    assert h.r.handle(payload(alert("HighErrorRate"), alert("HighLatencyP95"))) == "queued"
    # After the cooldown, the same alerts are investigated again.
    h.t += COOLDOWN_S + 1
    assert h.r.handle(payload(alert("HighErrorRate"))) == "queued"


def test_resolved_posts_notice_and_resets_group():
    h = Harness()
    h.r.handle(payload(alert("HighErrorRate")))
    assert h.r.handle(payload(alert("HighErrorRate", status="resolved"))) == "resolved"
    assert h.sent[-1]["embeds"][0]["title"] == "✅ Resolved: HighErrorRate"
    assert "08:55:02" in h.sent[-1]["embeds"][0]["description"]
    assert h.r.handle(payload(alert("HighErrorRate"))) == "queued"  # a recurrence is a new incident


def test_investigation_failure_is_reported():
    h = Harness()
    h.r.investigate = lambda q: (_ for _ in ()).throw(RuntimeError("groq unreachable"))
    h.r.handle(payload(alert("HighErrorRate")))
    h.r.work_once()
    assert h.sent[0]["embeds"][0]["title"] == "⚠️ Could not investigate HighErrorRate"
    assert "groq unreachable" in h.sent[0]["embeds"][0]["description"]


def test_long_answers_are_truncated_for_discord():
    msg = investigation_message(["X"], result(answer="a" * 5000, save="no id"), "m")
    embed = msg["embeds"][0]
    assert len(embed["description"]) < 4096 and embed["description"].endswith("(truncated)")
    assert embed["title"] == "🚨 X"  # no incident saved


def test_notifier_posts_and_retries_discord_rate_limit():
    calls, sleeps = [], []
    responses = iter([httpx.Response(429, json={"retry_after": 0.5}), httpx.Response(204)])

    def handler(req):
        calls.append(json.loads(req.content))
        return next(responses)

    n = Notifier("https://discord.test/webhook", http=httpx.Client(transport=httpx.MockTransport(handler)),
                 sleep=sleeps.append)
    n({"username": "GenBot", "embeds": [{"title": "t", "description": "d"}]})
    assert len(calls) == 2 and sleeps == [0.5]


def test_notifier_without_webhook_prints(capsys):
    Notifier(None)({"embeds": [{"title": "🚨 HighErrorRate", "description": "body"}]})
    assert "[discord dry-run] 🚨 HighErrorRate" in capsys.readouterr().out


def test_http_endpoint():
    h = Harness()
    server = make_server(h.r, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urllib.request.Request(f"{base}/alert", data=json.dumps(payload(alert("HighErrorRate"))).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert json.load(r) == {"action": "queued"}
        with urllib.request.urlopen(f"{base}/health") as r:
            assert json.load(r) == {"status": "ok"}
    finally:
        server.shutdown()


def test_investigations_are_logged_for_the_ui(tmp_path):
    log = tmp_path / "investigations.jsonl"
    h = Harness()
    h.r.log_path = log
    h.r.handle(payload(alert("HighErrorRate")))
    h.r.work_once()
    h.r.investigate = lambda q: (_ for _ in ()).throw(RuntimeError("down"))
    h.r.handle(payload(alert("MemoryGrowth")))
    h.r.work_once()
    first, second = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert first["alerts"] == ["HighErrorRate"] and first["incident"] == "INC-007"
    assert first["steps"][0] == {"tool": "service_health", "args": {}}
    assert second["alerts"] == ["MemoryGrowth"] and second["error"] == "RuntimeError: down"
