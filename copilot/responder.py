"""Alert responder: Alertmanager webhook -> GenBot investigation -> Discord.

    python -m copilot.responder     # listens on :8090 for Alertmanager

When alerts fire, GenBot investigates on its own and posts its diagnosis to Discord
(DISCORD_WEBHOOK_URL in .env), or to this console if no webhook is configured.
Investigations run one at a time in a background thread, so a burst of alerts never
runs several expensive LLM investigations at once.
"""
import json
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import httpx

from . import config
from .agent import Result
from .llm import PROVIDERS

COOLDOWN_S = 900  # don't re-investigate the same alerts within 15 minutes
DISCORD_MAX = 4000  # embed descriptions are capped at 4096 characters
RED, GREEN, GREY = 0xE74C3C, 0x2ECC71, 0x95A5A6


def utc(ts: str) -> str:
    """Alertmanager timestamps ('2026-11-01T08:47:27.123Z') -> 'HH:MM:SS'."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%H:%M:%S")
    except ValueError:
        return ts


def build_question(alerts: list[dict]) -> str:
    lines = []
    for a in alerts:
        labels, notes = a.get("labels", {}), a.get("annotations", {})
        lines.append(f"- {labels.get('alertname', '?')} ({labels.get('severity', 'unknown')}): "
                     f"{notes.get('summary', 'no summary')} (firing since {utc(a.get('startsAt', ''))} UTC)")
    return ("These Prometheus alerts just fired for ShopAPI:\n" + "\n".join(lines) +
            "\nInvestigate what is wrong right now and why.")


def incident_id(result: Result) -> str | None:
    """The incident the agent saved or updated, read from its save_postmortem result."""
    for step in result.steps:
        if step.tool == "save_postmortem" and (m := re.search(r"INC-\d+", step.result)):
            return m.group(0)
    return None


def investigation_message(names: list[str], result: Result, model: str) -> dict:
    inc = incident_id(result)
    answer = result.answer if len(result.answer) <= DISCORD_MAX else result.answer[:DISCORD_MAX] + "\n…(truncated)"
    return {"username": "GenBot", "embeds": [{
        "title": f"🚨 {', '.join(names)}" + (f" · {inc}" if inc else ""),
        "description": answer,
        "color": RED,
        "footer": {"text": f"{len(result.steps)} tool calls · {result.seconds:.0f}s · {result.model or model}"},
    }]}


def resolved_message(names: list[str], ended: str) -> dict:
    return {"username": "GenBot", "embeds": [{
        "title": f"✅ Resolved: {', '.join(names)}",
        "description": f"Alerts cleared at {ended} UTC. If GenBot saved an incident, record the fix that worked:\n"
                       "`python -m copilot.incidents resolve INC-… --fix \"…\"`",
        "color": GREEN,
    }]}


def error_message(names: list[str], error: Exception) -> dict:
    return {"username": "GenBot", "embeds": [{
        "title": f"⚠️ Could not investigate {', '.join(names)}",
        "description": f"{type(error).__name__}: {str(error)[:500]}",
        "color": GREY,
    }]}


class Notifier:
    """Posts to a Discord webhook, or prints when none is configured."""

    def __init__(self, webhook_url: str | None, http: httpx.Client | None = None, sleep=time.sleep):
        self.url, self.sleep = webhook_url, sleep
        self.http = http or httpx.Client(timeout=15)

    def __call__(self, message: dict) -> None:
        if not self.url:
            embed = message["embeds"][0]
            footer = f"\n[{embed['footer']['text']}]" if "footer" in embed else ""
            print(f"\n[discord dry-run] {embed['title']}\n{embed['description']}{footer}\n", flush=True)
            return
        for _ in range(3):
            r = self.http.post(self.url, json=message)
            if r.status_code == 429:  # Discord rate limit: retry_after is in seconds
                self.sleep(float(r.json().get("retry_after", 1)))
                continue
            r.raise_for_status()
            return


@dataclass
class _Seen:
    at: float
    names: frozenset[str]


class Responder:
    def __init__(self, investigate: Callable[[str], Result], notify: Callable[[dict], None], model: str = "",
                 clock=time.time):
        self.investigate, self.notify, self.model, self.clock = investigate, notify, model, clock
        self.jobs: queue.Queue = queue.Queue()
        self.seen: dict[str, _Seen] = {}
        self.lock = threading.Lock()

    def handle(self, payload: dict) -> str:
        """Process one Alertmanager webhook. Returns what was done (for logs and tests)."""
        alerts = payload.get("alerts", [])
        key = payload.get("groupKey", "default")
        firing = [a for a in alerts if a.get("status") == "firing"]

        if not firing:
            names = sorted({a.get("labels", {}).get("alertname", "?") for a in alerts})
            ended = max((utc(a.get("endsAt", "")) for a in alerts), default="?")
            with self.lock:
                self.seen.pop(key, None)
            self.notify(resolved_message(names, ended))
            return "resolved"

        names = frozenset(a.get("labels", {}).get("alertname", "?") for a in firing)
        now = self.clock()
        with self.lock:
            prev = self.seen.get(key)
            # Alertmanager re-sends a group when anything changes; only new alert names are new information.
            if prev and now - prev.at < COOLDOWN_S and names <= prev.names:
                return "skipped: already investigated"
            self.seen[key] = _Seen(now, names | (prev.names if prev else frozenset()))
        self.jobs.put((sorted(names), build_question(firing)))
        return "queued"

    def work_once(self) -> None:
        names, question = self.jobs.get()
        try:
            result = self.investigate(question)
            self.notify(investigation_message(names, result, self.model))
        except Exception as e:  # report the failure instead of dying silently
            self.notify(error_message(names, e))
        finally:
            self.jobs.task_done()

    def start_worker(self) -> threading.Thread:
        def loop():
            while True:
                self.work_once()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t


def make_server(responder: Responder, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/alert":
                return self._reply(404, {"error": "not found"})
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except json.JSONDecodeError:
                return self._reply(400, {"error": "invalid JSON"})
            action = responder.handle(payload)
            print(f"[responder] {payload.get('status')} {[a['labels'].get('alertname') for a in payload.get('alerts', [])]}"
                  f" -> {action}", flush=True)
            self._reply(200, {"action": action})

        def do_GET(self):
            self._reply(200, {"status": "ok"}) if self.path == "/health" else self._reply(404, {"error": "not found"})

        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # requests are logged in do_POST instead
            pass

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from .ask import build_agent

    def investigate(question: str) -> Result:
        print(f"[responder] investigating:\n{question}", flush=True)
        agent = build_agent(config.LLM_PROVIDER, config.LLM_MODEL,
                            on_step=lambda s: print(f"  -> {s.tool}({s.args})", flush=True))
        return agent.ask(question)

    model = config.LLM_MODEL or PROVIDERS[config.LLM_PROVIDER][2]
    responder = Responder(investigate, Notifier(config.DISCORD_WEBHOOK_URL), model=model)
    responder.start_worker()
    server = make_server(responder, config.RESPONDER_PORT)
    target = "Discord" if config.DISCORD_WEBHOOK_URL else "console (set DISCORD_WEBHOOK_URL to post to Discord)"
    print(f"[responder] listening on :{config.RESPONDER_PORT}/alert, posting to {target}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[responder] stopped")


if __name__ == "__main__":
    main()
