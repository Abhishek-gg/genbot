import httpx
import pytest

from copilot.logstore import LogStore
from copilot.tailer import Checkpoint
from copilot.tools import Toolbox, classify, describe_series, hhmmss, parse_args

NOW = 1_800_000_600.0


def series(values, **labels):
    return {"metric": labels, "values": [[NOW - 600 + 15 * i, str(v)] for i, v in enumerate(values)]}


def prometheus(results_by_query):
    """Fake Prometheus: picks a canned result by substring of the query."""
    def handler(req: httpx.Request):
        if req.url.path == "/api/v1/alerts":
            return httpx.Response(200, json={"data": {"alerts": [
                {"labels": {"alertname": "HighErrorRate"}, "state": "firing"}]}})
        q = req.url.params["query"]
        if "(((" in q:
            return httpx.Response(400, json={"status": "error", "error": "parse error"})
        for key, result in results_by_query.items():
            if key in q:
                return httpx.Response(200, json={"status": "success", "data": {"result": result}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})
    return handler


@pytest.fixture
def toolbox(tmp_path):
    def make(handler=None, store=None):
        return Toolbox("http://prom", index=None, store=store or LogStore(tmp_path / "l.db"),
                       http=httpx.Client(transport=httpx.MockTransport(handler or prometheus({}))),
                       clock=lambda: NOW)
    return make


def test_describe_series():
    flat = [(NOW + i, 5.0) for i in range(10)]
    assert describe_series(flat) == "stable around 5"
    noisy = [(NOW + i, v) for i, v in enumerate([0.1, 0.2, 0.12, 0.18])]
    assert describe_series(noisy).startswith("normal fluctuation 0.1 to 0.2")
    spike = [(NOW + i, v) for i, v in enumerate([0, 0, 0, 0.4, 0.5])]
    d = describe_series(spike)
    assert "now 0.5" in d and "max 0.5" in d and "rose sharply" in d
    assert describe_series([(NOW, float("nan"))]) == "no data"


def ending_now(values, **labels):
    """A series sampled every 15s whose last point is NOW."""
    n = len(values)
    return {"metric": labels, "values": [[NOW - 15 * (n - 1 - i), str(v)] for i, v in enumerate(values)]}


def pts(values):
    return [(float(t), float(v)) for t, v in ending_now(values)["values"]]


def test_classify_separates_now_from_recovered():
    # Error ratio: normal, spike, recovered, then a new episode in the last 2 minutes.
    status, text, onset = classify(pts([0] * 10 + [0.3] * 4 + [0] * 10 + [0.09] * 9), floor=0.02, now=NOW)
    assert status == "now" and onset == NOW - 15 * 8  # onset of the *current* episode, not the first spike
    assert f"abnormal since ~{hhmmss(onset)}" in text and "peak 0.3" in text

    status, text, onset = classify(pts([0] * 10 + [2.0] * 5 + [0.02] * 15), floor=0.1, now=NOW)
    assert status == "earlier" and text.startswith("peaked 2 at") and onset is None

    assert classify(pts([0.1, 0.2, 0.12, 0.18] * 5), floor=0.5, now=NOW)[0] == "normal"  # noise, not an incident
    # Abnormal for the whole window: the usual level is itself past the floor.
    status, text, _ = classify(pts([0.6] * 20), floor=0.3, now=NOW)
    assert status == "now" and "whole window" in text


def test_service_health_groups_signals(toolbox):
    tb = toolbox(prometheus({
        "status=~\"5..\"}[1m])) /": [ending_now([0] * 22 + [0.09] * 8)],
        "dependency_errors_total": [ending_now([0] * 20 + [0.5] * 10, dependency="payment-gateway")],
        "db_query_duration_seconds_bucket": [ending_now([0.02] * 10 + [2.0] * 5 + [0.02] * 15, table="orders", op="insert")],
        "process_cpu_seconds_total": [ending_now([0.01] * 30)],
        "sum(rate(http_requests_total[1m]))": [ending_now([5.0] * 30)],
    }))
    out = tb.run("service_health", {"minutes": 10})
    # Dependency errors began 30s before the 5xx: listed first and named as the first signal.
    assert (f"CURRENT INCIDENT: began ~{hhmmss(NOW - 15 * 9)} UTC; first abnormal signal: Dependency errors (/s) "
            "dependency=payment-gateway. Pass minutes=4 to log tools") in out
    now_section = out.split("ABNORMAL NOW")[1].split("Abnormal earlier")[0]
    assert now_section.index("Dependency errors") < now_section.index("5xx error ratio")
    assert "5xx error ratio: now 0.08" in now_section  # 2-min average includes the last normal sample
    assert "Dependency errors (/s) dependency=payment-gateway: now 0.5" in now_section
    assert "DB p95" not in now_section
    assert "RECOVERED" in out and "DB p95 query time (s) op=insert,table=orders: peaked 2" in out
    assert "Normal: Request rate (req/s): stable around 5; CPU (cores): 0.01" in out
    assert out.endswith("Alerts: HighErrorRate (firing)")


def test_service_health_when_all_normal(toolbox):
    out = toolbox(prometheus({"process_cpu_seconds_total": [ending_now([0.01] * 30)]})).run("service_health", {})
    assert "ABNORMAL NOW: nothing" in out and "RECOVERED" not in out


def test_query_metrics_returns_prometheus_errors_as_text(toolbox):
    out = toolbox().run("query_metrics", {"promql": "(((bad"})
    assert out.startswith("error: Prometheus rejected the query")


def test_unknown_tool_and_bad_args(toolbox):
    tb = toolbox()
    assert tb.run("nope", {}).startswith("error: unknown tool")
    assert tb.run("service_health", {"bogus": 1}).startswith("error: bad arguments")


def test_get_log_lines_filters_and_shows_newest(toolbox, tmp_path):
    store = LogStore(tmp_path / "logs.db")
    rows = [{"ts": NOW - 60 + i, "level": "ERROR", "version": "v1", "msg": f"Payment failed #{i}",
             "trace_id": f"t{i}", "route": None, "status": None, "duration_ms": None, "raw": "{}"} for i in range(5)]
    rows.append({**rows[0], "level": "INFO", "msg": "GET /products 200", "route": "/products", "status": 200,
                 "duration_ms": 12})
    store.insert(rows, "shopapi", Checkpoint())

    out = toolbox(store=store).run("get_log_lines", {"level": "ERROR", "contains": "Payment", "limit": 2})
    assert out.splitlines()[0] == "5 matching lines, showing the last 2:"
    assert "Payment failed #4" in out and "Payment failed #2" not in out


def test_parse_args_tolerates_empty():
    assert parse_args("") == {} and parse_args("null") == {} and parse_args('{"minutes": 5}') == {"minutes": 5}


def test_specs_are_valid_tool_schemas():
    names = [s["function"]["name"] for s in Toolbox("http://prom", None, None).specs()]
    assert names == ["service_health", "query_metrics", "search_logs", "get_log_lines", "get_recent_deploys"]


def test_unexpected_tool_failure_is_returned_as_text(toolbox):
    class Broken:
        def search(self, *a, **k):
            raise RuntimeError("index unavailable")

    tb = toolbox()
    tb.index = Broken()
    assert tb.run("search_logs", {"query": "x"}) == (
        "error: search_logs failed (RuntimeError: index unavailable). Try another tool.")
