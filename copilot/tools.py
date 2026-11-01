"""The agent's tools: live metrics, log search, raw log lines and deploy history.

Every tool returns short plain text. The free LLM tiers allow only a few thousand
tokens per minute, so tools summarize (min/max/when it changed) instead of dumping
raw series. Errors are returned as text so the model can correct itself.
"""
import json
import math
import time
from datetime import datetime, timezone

import httpx

from .logstore import LogStore
from .memory import IncidentMemory
from .vectorstore import WindowIndex

MAX_SERIES = 8
NOISE_RATIO = 2.5  # swings smaller than this (max/min) are reported as normal fluctuation

CURRENT_S = 120  # "now" means the average of the last two minutes...
LATEST_S = 60  # ...provided the last minute is still abnormal too

# (title, PromQL, floor). A value is abnormal above the floor, or above NOISE_RATIO x its usual level
# if that is higher. A floor of None means informational only (the request rate is never "abnormal").
HEALTH_QUERIES = [
    ("Request rate (req/s)", "sum(rate(http_requests_total[1m]))", None),
    ("5xx error ratio", 'sum(rate(http_requests_total{status=~"5.."}[1m])) / sum(rate(http_requests_total[1m]))', 0.02),
    ("p95 latency (s)", "histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket[1m])))", 0.5),
    ("5xx rate (req/s)", 'sum by (route) (rate(http_requests_total{status=~"5.."}[1m]))', 0.02),
    ("DB p95 query time (s)", "histogram_quantile(0.95, sum by (le, table, op) (rate(db_query_duration_seconds_bucket[1m])))", 0.1),
    ("Dependency errors (/s)", "sum by (dependency) (rate(dependency_errors_total[1m]))", 0.02),
    ("CPU (cores)", 'rate(process_cpu_seconds_total{job="shopapi"}[1m])', 0.3),
    ("Memory RSS (MB)", 'process_resident_memory_bytes{job="shopapi"} / 1e6', 150),
]


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def fmt(v: float) -> str:
    if v == 0:
        return "0"
    if abs(v) >= 100:
        return f"{v:.0f}"
    return f"{v:.3g}"


def describe_series(points: list[tuple[float, float]]) -> str:
    """Compress a time series to: current value, range, and when it changed."""
    pts = [(t, v) for t, v in points if not math.isnan(v)]
    if not pts:
        return "no data"
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    now = vals[-1]
    if hi - lo <= max(1e-3, 0.25 * max(abs(lo), abs(hi))):
        return f"stable around {fmt(now)}"
    if lo > 0 and hi / lo < NOISE_RATIO:
        return f"normal fluctuation {fmt(lo)} to {fmt(hi)}, now {fmt(now)}"
    t_lo = next(t for t, v in pts if v == lo)
    t_hi = next(t for t, v in pts if v == hi)
    first = vals[0]
    # First point that moved more than half the range away from where the window started.
    change = next(((t, v) for t, v in pts if abs(v - first) > (hi - lo) / 2), None)
    parts = [f"now {fmt(now)}", f"min {fmt(lo)} at {hhmmss(t_lo)}", f"max {fmt(hi)} at {hhmmss(t_hi)}"]
    if change is not None:
        parts.append(f"{'rose' if change[1] > first else 'fell'} sharply at ~{hhmmss(change[0])}")
    return ", ".join(parts)


def classify(points: list[tuple[float, float]], floor: float, now: float) -> tuple[str, str, float | None] | None:
    """Sort a series into 'now' (abnormal at present), 'earlier' (abnormal, since recovered) or 'normal'.

    Returns (status, description, onset), where onset is when the current episode began ('now' only).
    Keeping these apart matters: a problem that already recovered is history, and blaming it for
    the current incident is the most common misdiagnosis.
    """
    pts = [(t, v) for t, v in points if not math.isnan(v)]
    if not pts:
        return None
    vals = sorted(v for _, v in pts)
    usual = vals[len(vals) // 10]  # 10th percentile: the series' normal level
    # If even the usual level is past the floor, the whole window is abnormal; judge by the floor alone.
    threshold = floor if usual >= floor else max(floor, NOISE_RATIO * usual)
    recent = [v for t, v in pts if t >= now - CURRENT_S] or [pts[-1][1]]
    current = sum(recent) / len(recent)
    latest = [v for t, v in pts if t >= now - LATEST_S] or [pts[-1][1]]
    t_peak, peak = max(pts, key=lambda p: p[1])

    # A high 2-minute average whose latest samples are all normal again is a recovery in progress,
    # not a current problem (the average lags behind a fault that has just ended).
    if current > threshold and max(latest) > threshold:
        onset = None  # start of the episode that is still going on
        for t, v in pts:
            if v <= threshold:
                onset = None
            elif onset is None:
                onset = t
        if onset is None:  # recent average is high but the very last sample dipped
            onset = max(t for t, v in pts if v > threshold)
        since = f"since ~{hhmmss(onset)}" if onset != pts[0][0] else f"since before {hhmmss(onset)} (whole window)"
        text = f"now {fmt(current)} (normal ~{fmt(usual)}), abnormal {since}, peak {fmt(peak)} at {hhmmss(t_peak)}"
        return "now", text, onset
    if peak > threshold:
        last_bad = max(t for t, v in pts if v > threshold)
        return "earlier", f"peaked {fmt(peak)} at {hhmmss(t_peak)}, normal again after {hhmmss(last_bad)}, now {fmt(latest[-1])}", None
    return "normal", fmt(current), None


def series_label(metric: dict) -> str:
    labels = {k: v for k, v in metric.items() if k not in ("__name__", "job", "instance")}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items())) or "total"


class Toolbox:
    def __init__(
        self,
        prometheus_url: str,
        index: WindowIndex,
        store: LogStore,
        github_repo: str | None = None,
        github_token: str | None = None,
        memory: IncidentMemory | None = None,
        http: httpx.Client | None = None,
        clock=time.time,
    ):
        self.prom = prometheus_url.rstrip("/")
        self.index, self.store, self.memory = index, store, memory
        self.repo, self.github_token = github_repo, github_token
        self.http = http or httpx.Client(timeout=20)
        self.clock = clock
        self._fns = {
            "service_health": self.service_health,
            "query_metrics": self.query_metrics,
            "search_logs": self.search_logs,
            "get_log_lines": self.get_log_lines,
            "get_recent_deploys": self.get_recent_deploys,
        }
        if memory is not None:
            self._fns["search_past_incidents"] = self.search_past_incidents
            self._fns["save_postmortem"] = self.save_postmortem

    def run(self, name: str, args: dict) -> str:
        fn = self._fns.get(name)
        if fn is None:
            return f"error: unknown tool {name!r}. Available: {', '.join(self._fns)}"
        try:
            return fn(**args)
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"
        except httpx.HTTPError as e:
            return f"error: {name} could not reach its data source: {e}"
        except Exception as e:  # a broken data source must not end the investigation
            return f"error: {name} failed ({type(e).__name__}: {e}). Try another tool."

    # --- Prometheus -------------------------------------------------------------

    def _range(self, promql: str, minutes: float, points: int = 40) -> list[dict]:
        end = self.clock()
        start = end - minutes * 60
        step = max(15, int(minutes * 60 / points))
        r = self.http.get(f"{self.prom}/api/v1/query_range",
                          params={"query": promql, "start": start, "end": end, "step": step})
        body = r.json()
        if body.get("status") != "success":
            raise ValueError(body.get("error", r.text[:300]))
        return body["data"]["result"]

    def _describe(self, results: list[dict]) -> list[str]:
        lines = []
        for s in results[:MAX_SERIES]:
            pts = [(float(t), float(v)) for t, v in s["values"]]
            lines.append(f"  {series_label(s['metric'])}: {describe_series(pts)}")
        if len(results) > MAX_SERIES:
            lines.append(f"  ...{len(results) - MAX_SERIES} more series")
        return lines

    def service_health(self, minutes: float = 30) -> str:
        now = self.clock()
        groups: dict[str, list[str]] = {"earlier": [], "normal": []}
        current: list[tuple[float, str]] = []  # (onset, line) for signals abnormal now
        failed = []
        for title, q, floor in HEALTH_QUERIES:
            try:
                res = self._range(q, minutes)
            except ValueError as e:
                failed.append(f"{title}: query failed: {e}")
                continue
            for s in res:
                pts = [(float(t), float(v)) for t, v in s["values"]]
                label = series_label(s["metric"])
                name = title if label == "total" else f"{title} {label}"
                if floor is None:
                    groups["normal"].append(f"{name}: {describe_series(pts)}")
                elif (c := classify(pts, floor, now)) is not None:
                    status, text, onset = c
                    if status == "now":
                        current.append((onset, f"{name}: {text}"))
                    else:
                        groups[status].append(f"{name}: {text}")

        out = [f"Service health, last {minutes:g} min (now {hhmmss(now)} UTC; 'now' = average of the last 2 min)."]
        if current:
            current.sort()
            start, first = current[0]
            since_min = math.ceil((now - start) / 60) + 1
            out.append(f"CURRENT INCIDENT: began ~{hhmmss(start)} UTC; first abnormal signal: {first.split(':')[0]}. "
                       f"Pass minutes={since_min} to log tools to see only this incident.")
            out.append("ABNORMAL NOW (earliest first; later ones are often effects of earlier ones):")
            out.extend(f"- {line}" for _, line in current)
        else:
            out.append("ABNORMAL NOW: nothing")
        if groups["earlier"]:
            out.append("Abnormal earlier in the window but RECOVERED (past, not the current problem):")
            out.extend(f"- {line}" for line in groups["earlier"])
        out.append("Normal: " + "; ".join(groups["normal"]))
        out.extend(failed)
        alerts = self.http.get(f"{self.prom}/api/v1/alerts").json()["data"]["alerts"]
        active = sorted({f"{a['labels']['alertname']} ({a['state']})" for a in alerts})
        out.append("Alerts: " + (", ".join(active) if active else "none"))
        return "\n".join(out)

    def query_metrics(self, promql: str, minutes: float = 15) -> str:
        try:
            res = self._range(promql, minutes)
        except ValueError as e:
            return f"error: Prometheus rejected the query: {e}"
        if not res:
            return "no series matched"
        return "\n".join([f"{promql} over last {minutes:g} min:"] + self._describe(res))

    # --- Logs -------------------------------------------------------------------

    def search_logs(self, query: str, minutes: float = 30, only_issues: bool = True, k: int = 3) -> str:
        hits = self.index.search(query, start=self.clock() - minutes * 60, only_issues=only_issues, k=min(k, 5))
        if not hits:
            return "no matching log windows" + (" with warnings or errors" if only_issues else "")
        return "\n\n".join(h["document"] for h in sorted(hits, key=lambda h: h["start"]))

    def get_log_lines(
        self,
        minutes: float = 10,
        level: str | None = None,
        route: str | None = None,
        contains: str | None = None,
        trace_id: str | None = None,
        limit: int = 15,
    ) -> str:
        now = self.clock()
        rows = self.store.query(now - minutes * 60, now + 1, level=level, route=route, contains=contains,
                                trace_id=trace_id)
        if not rows:
            return "no matching log lines"
        limit = min(limit, 30)
        picked = rows[-limit:]  # most recent
        lines = [f"{len(rows)} matching lines, showing the last {len(picked)}:"]
        for r in picked:
            extra = f" route={r['route']} status={r['status']} {r['duration_ms']}ms" if r["status"] else ""
            trace = f" trace={r['trace_id']}" if r["trace_id"] else ""
            lines.append(f"{hhmmss(r['ts'])} {r['level']} {r['msg'][:160]}{extra}{trace}")
        return "\n".join(lines)

    # --- Deploys ----------------------------------------------------------------

    def get_recent_deploys(self, hours: float = 24) -> str:
        out = []
        # What actually ran: version changes seen by Prometheus.
        try:
            res = self._range("max by (version) (shopapi_build_info)", hours * 60, points=1000)
            spans = sorted((float(s["values"][0][0]), float(s["values"][-1][0]), s["metric"].get("version", "?"))
                           for s in res)
            out.append("Running versions (from metrics):")
            out.extend(f"  {v}: seen {hhmmss(a)} to {hhmmss(b)} UTC" for a, b, v in spans)
            if len(spans) <= 1:
                out.append("  no version change in this period")
        except ValueError as e:
            out.append(f"Running versions: query failed: {e}")

        # What was committed: GitHub history.
        if self.repo:
            since = datetime.fromtimestamp(self.clock() - hours * 3600, tz=timezone.utc).isoformat()
            headers = {"Accept": "application/vnd.github+json"}
            if self.github_token:
                headers["Authorization"] = f"Bearer {self.github_token}"
            r = self.http.get(f"https://api.github.com/repos/{self.repo}/commits",
                              params={"since": since, "per_page": 10}, headers=headers)
            if r.status_code == 404:
                out.append("GitHub commits: unavailable (repository is private or not found; set GITHUB_TOKEN)")
            elif r.status_code != 200:
                out.append(f"GitHub commits: unavailable ({r.status_code})")
            else:
                commits = r.json()
                out.append(f"Commits to {self.repo} in the last {hours:g}h: " + ("none" if not commits else ""))
                for c in commits:
                    date = c["commit"]["committer"]["date"]
                    first_line = c["commit"]["message"].splitlines()[0][:100]
                    out.append(f"  {date} {c['sha'][:7]} {first_line}")
        return "\n".join(out)

    # --- Incident memory -----------------------------------------------------------

    def search_past_incidents(self, symptoms: str, k: int = 3) -> str:
        hits = self.memory.search(symptoms, k=min(k, 5))
        if not hits:
            return "No similar past incidents."
        out = ["Past incidents, most similar first:"]
        for inc, similarity in hits:
            if inc.status == "resolved":
                status = f"RESOLVED by an engineer. Fix that worked: {inc.resolution}"
            else:
                status = f"OPEN, diagnosis not yet confirmed by an engineer. Suggested fix: {inc.suggested_fix or 'none'}"
            out.append(f"- {inc.id} ({inc.date}, similarity {similarity}): {inc.title}\n"
                       f"  Symptoms: {inc.symptoms}\n  Root cause: {inc.root_cause}\n  {status}")
        return "\n".join(out)

    def save_postmortem(self, title: str, symptoms: str, root_cause: str, suggested_fix: str = "",
                        confidence: str = "") -> str:
        inc, created = self.memory.save(title, symptoms, root_cause, suggested_fix, confidence)
        if created:
            return f"Saved as {inc.id} (open until an engineer confirms the fix)."
        return f"Updated {inc.id}: it is the same ongoing incident, so no duplicate was created."

    # --- Schemas ----------------------------------------------------------------

    def specs(self) -> list[dict]:
        def tool(name, description, properties, required=()):
            return {"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": list(required)}}}

        text = {"type": "string"}
        minutes = {"type": "number", "description": "How far back to look, in minutes."}
        memory_tools = [
            tool("search_past_incidents",
                 "Search postmortems of past incidents by symptoms, e.g. '5xx on /orders, payment-gateway "
                 "dependency errors'. Resolved incidents include the fix that worked.",
                 {"symptoms": text, "k": {"type": "integer", "description": "Max results, up to 5."}}, ["symptoms"]),
            tool("save_postmortem",
                 "Save your diagnosis of the current incident to long-term memory. Call it once, at the end, "
                 "only when something is wrong now.",
                 {"title": {"type": "string", "description": "Short title, e.g. 'Payment gateway timeouts on /orders'"},
                  "symptoms": {"type": "string", "description": "What was abnormal, with key numbers"},
                  "root_cause": text, "suggested_fix": text,
                  "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
                 ["title", "symptoms", "root_cause"]),
        ] if self.memory is not None else []
        return [
            tool("service_health",
                 "Overview of ShopAPI from live Prometheus metrics: traffic, 5xx ratio, p95 latency per route, "
                 "DB query time, dependency errors, CPU, memory and firing alerts. Separates what is abnormal "
                 "NOW (and since when) from problems earlier in the window that have recovered. Call this first.",
                 {"minutes": minutes}),
            tool("query_metrics",
                 "Run a custom PromQL range query for details service_health does not show.",
                 {"promql": {"type": "string"}, "minutes": minutes}, ["promql"]),
            tool("search_logs",
                 "Semantic search over 30-second log summaries (traffic, latency, distinct warnings and errors "
                 "with counts). Use natural language, e.g. 'payment errors' or 'slow database queries'.",
                 {"query": {"type": "string"}, "minutes": minutes,
                  "only_issues": {"type": "boolean", "description": "Only windows with warnings, errors or 5xx (default true)."},
                  "k": {"type": "integer", "description": "Number of windows, max 5."}},
                 ["query"]),
            tool("get_log_lines",
                 "Fetch raw log lines, newest last, filtered by level, route, message text or trace ID. "
                 "Use it to read exact error messages and stack details.",
                 {"minutes": minutes,
                  "level": {"type": "string", "enum": ["INFO", "WARNING", "ERROR"]},
                  "route": {"type": "string", "description": "Route template, e.g. /orders"},
                  "contains": {"type": "string", "description": "Substring of the message"},
                  "trace_id": {"type": "string"},
                  "limit": {"type": "integer", "description": "Max lines, up to 30."}}),
            tool("get_recent_deploys",
                 "Which ShopAPI versions ran and when (from metrics), plus recent commits on GitHub. "
                 "Use it to check whether a deploy lines up with when a problem started.",
                 {"hours": {"type": "number", "description": "How far back to look, in hours."}}),
        ] + memory_tools


def parse_args(raw: str) -> dict:
    """Tool-call arguments arrive as a JSON string; models sometimes send '' or null."""
    if not raw or raw == "null":
        return {}
    args = json.loads(raw)
    return args if isinstance(args, dict) else {}
