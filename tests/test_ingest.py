"""End to end: log file -> SQLite + Chroma -> semantic search."""
import json
from datetime import datetime, timezone

import pytest

from copilot.ingest import Ingester
from copilot.logstore import LogStore, parse_line
from copilot.tailer import Tailer
from copilot.vectorstore import WindowIndex

T0 = 1_800_000_000.0  # a 30s-aligned epoch


def line(ts, level="INFO", msg="GET /products 200 12ms", **fields):
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return json.dumps({"ts": iso, "level": level, "service": "shopapi", "version": "v1", "msg": msg, **fields}) + "\n"


@pytest.fixture
def setup(tmp_path):
    log = tmp_path / "shopapi.log"
    log.touch()
    store = LogStore(tmp_path / "logs.db")
    ing = Ingester(store, WindowIndex(tmp_path / "chroma"), Tailer(log), window_s=30)
    return log, store, ing


def test_parse_line_keeps_exception_type():
    row = parse_line(line(T0, "ERROR", "Payment failed", exc_type="PaymentGatewayError", trace_id="abc"))
    assert row["msg"] == "Payment failed [PaymentGatewayError]"
    assert row["trace_id"] == "abc" and row["ts"] == T0
    assert parse_line("not json") is None


def test_ingest_and_search(setup):
    log, store, ing = setup
    with open(log, "a") as f:
        for i in range(10):  # healthy window
            f.write(line(T0 + i, route="/products", status=200, duration_ms=12))
        for i in range(10):  # failing window
            f.write(line(T0 + 30 + i, route="/orders", status=502, duration_ms=80))
            f.write(line(T0 + 30 + i, "ERROR", "Payment failed: Timeout contacting payment-gateway after 5000ms",
                         exc_type="PaymentGatewayError"))
        f.write("garbage line\n")

    assert ing.poll() == 30
    assert ing.flush(now=T0 + 40) == 1  # only the first window has closed
    assert ing.flush(now=None) == 1

    hits = ing.index.search("payment provider errors", k=1)
    assert hits[0]["start"] == T0 + 30 and hits[0]["n_5xx"] == 10

    assert [h["start"] for h in ing.index.search("anything", only_issues=True, k=5)] == [T0 + 30]
    assert ing.index.search("anything", start=T0 + 60, k=5) == []
    assert len(store.query(T0, T0 + 60, level="error")) == 10


def test_checkpoint_prevents_duplicates_after_restart(setup):
    log, store, ing = setup
    log.write_text(line(T0) + line(T0 + 1))
    ing.poll()
    restarted = Tailer(log, store.checkpoint("shopapi"))
    assert restarted.read_new() == []
    assert len(store.query(T0, T0 + 30)) == 2


def test_reindex_marks_every_stored_window(setup):
    log, store, ing = setup
    log.write_text("".join(line(T0 + t) for t in (0, 5, 31, 95)))  # windows T0, T0+30, T0+90
    ing.poll()
    ing.flush(None)
    ing.recover(full=True)
    assert ing.dirty == {T0, T0 + 30, T0 + 90}
