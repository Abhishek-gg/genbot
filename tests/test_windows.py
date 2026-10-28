from copilot.windows import summarize, template, window_start


def req(ts, route="/products", status=200, ms=10, level="INFO", msg="req"):
    return {"ts": ts, "level": level, "version": "v1.0.0", "msg": msg, "route": route, "status": status, "duration_ms": ms}


def event(ts, level, msg):
    return {"ts": ts, "level": level, "version": "v1.0.0", "msg": msg, "route": None, "status": None, "duration_ms": None}


def test_template_collapses_numbers_and_ids():
    assert template("Slow query on orders.insert took 738ms") == "Slow query on orders.insert took <N>ms"
    assert template("trace 104cfeb7e6bb48d8 failed") == "trace <ID> failed"
    # Long digit runs are numbers, not hex IDs.
    assert template("amount=3711.6000000000004") == "amount=<N>"


def test_window_start_aligns_to_boundary():
    assert window_start(95.0, 30) == 90.0
    assert window_start(90.0, 30) == 90.0


def test_summary_counts_and_groups_repeated_messages():
    rows = [req(i, ms=10) for i in range(18)] + [
        req(18, route="/orders", status=502, ms=900),
        req(19, route="/orders", status=502, ms=1200),
        event(18, "ERROR", "Payment failed: Timeout after 5000ms (amount=100.5)"),
        event(19, "ERROR", "Payment failed: Timeout after 5000ms (amount=7.25)"),
        event(20, "WARNING", "Slow query on orders.insert took 812ms"),
    ]
    doc, meta = summarize(rows, 0, 30)

    assert meta["n_requests"] == 20 and meta["n_5xx"] == 2
    assert meta["n_errors"] == 2 and meta["n_warnings"] == 1
    assert meta["has_issues"] is True
    assert meta["p95_ms"] == 1200
    assert "ERROR x2: Payment failed: Timeout after <N>ms (amount=<N>)" in doc
    assert "5xx by route: /orders x2" in doc


def test_healthy_window_says_so():
    doc, meta = summarize([req(1), req(2)], 0, 30)
    assert "No warnings or errors." in doc
    assert meta["has_issues"] is False


def test_empty_window():
    doc, meta = summarize([], 0, 30)
    assert "No requests served." in doc
    assert meta["p95_ms"] == 0
