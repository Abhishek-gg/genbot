"""Turn the raw lines of one time window into a short, embeddable summary.

Embedding every request line would drown the index in near-identical "GET /products 200"
entries. Instead each window becomes one document: traffic stats plus the distinct
warnings and errors, with variable parts (numbers, IDs) templated out so repeats collapse.
"""
import re
from collections import Counter
from datetime import datetime, timezone

_ID = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")  # hex IDs; pure digit runs are numbers
_NUM = re.compile(r"\d+(\.\d+)?")
MAX_MESSAGES = 10


def window_start(ts: float, window_s: int) -> float:
    return ts - (ts % window_s)


def template(msg: str) -> str:
    return _NUM.sub("<N>", _ID.sub("<ID>", msg))


def _pct(sorted_vals: list[int], p: float) -> int:
    return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def summarize(rows: list[dict], start: float, end: float) -> tuple[str, dict]:
    """Return (document text, metadata) for the lines in [start, end)."""
    requests = [r for r in rows if r.get("status") is not None and r.get("duration_ms") is not None]
    statuses = Counter(r["status"] // 100 for r in requests)
    durations = sorted(r["duration_ms"] for r in requests)
    issues = Counter((r["level"], template(r["msg"] or "")) for r in rows if r["level"] in ("WARNING", "ERROR"))
    versions = sorted({r["version"] for r in rows if r.get("version")})

    lines = [f"Logs from {_fmt(start)} to {_fmt(end)} UTC, version {', '.join(versions) or 'unknown'}."]
    if requests:
        lines.append(
            f"Requests: {len(requests)} total, {statuses[2]} 2xx, {statuses[4]} 4xx, {statuses[5]} 5xx."
        )
        lines.append(f"Latency: p50 {_pct(durations, 0.5)}ms, p95 {_pct(durations, 0.95)}ms, max {durations[-1]}ms.")
        by_route: dict[str, list[int]] = {}
        for r in requests:
            by_route.setdefault(f"{r['route']}", []).append(r["duration_ms"])
        slowest = sorted(((max(d), route) for route, d in by_route.items()), reverse=True)[:3]
        lines.append("Slowest routes: " + "; ".join(f"{route} max {ms}ms" for ms, route in slowest) + ".")
        failing = Counter(r["route"] for r in requests if r["status"] >= 500)
        if failing:
            lines.append("5xx by route: " + "; ".join(f"{route} x{n}" for route, n in failing.most_common()) + ".")
    else:
        lines.append("No requests served.")

    if issues:
        lines.append("Warnings and errors:")
        for (level, msg), n in issues.most_common(MAX_MESSAGES):
            lines.append(f"- {level} x{n}: {msg}")
        if len(issues) > MAX_MESSAGES:
            lines.append(f"- ...and {len(issues) - MAX_MESSAGES} more distinct messages")
    else:
        lines.append("No warnings or errors.")

    meta = {
        "start": start,
        "end": end,
        "n_lines": len(rows),
        "n_requests": len(requests),
        "n_5xx": statuses[5],
        "n_errors": sum(n for (lvl, _), n in issues.items() if lvl == "ERROR"),
        "n_warnings": sum(n for (lvl, _), n in issues.items() if lvl == "WARNING"),
        "p95_ms": _pct(durations, 0.95) if durations else 0,
        "versions": ",".join(versions),
        "has_issues": bool(issues) or statuses[5] > 0,
    }
    return "\n".join(lines), meta
