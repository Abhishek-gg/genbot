import pytest

from copilot.memory import DEDUP_S, IncidentMemory
from copilot.tools import Toolbox

T0 = 1_800_000_000.0

PAYMENT = dict(title="Payment gateway timeouts on /orders",
               symptoms="5xx on POST /orders at 9%, payment-gateway dependency errors 0.45/s, other routes healthy",
               root_cause="External payment gateway timing out", suggested_fix="Fail over to backup provider",
               confidence="high")
DB = dict(title="Slow database queries on all routes",
          symptoms="DB p95 query time 2.3s on orders and products, p95 latency above 2s on every route, no 5xx",
          root_cause="Database slowdown", suggested_fix="Check DB host I/O and locks", confidence="high")


@pytest.fixture
def mem(tmp_path):
    clock = {"t": T0}
    m = IncidentMemory(tmp_path / "inc.db", tmp_path / "chroma", clock=lambda: clock["t"])
    m.clock_state = clock
    return m


def test_save_assigns_sequential_ids(mem):
    a, created_a = mem.save(**PAYMENT)
    b, created_b = mem.save(**DB)
    assert (a.id, b.id) == ("INC-001", "INC-002") and created_a and created_b
    assert a.status == "open" and mem.get("inc-001").title == PAYMENT["title"]


def test_search_finds_the_similar_incident(mem):
    mem.save(**PAYMENT)
    mem.save(**DB)
    hits = mem.search("checkout returning 502, payment-gateway errors on /orders")
    assert hits[0][0].id == "INC-001"


def test_same_ongoing_incident_is_updated_not_duplicated(mem):
    first, _ = mem.save(**PAYMENT)
    mem.clock_state["t"] += 300
    again, created = mem.save(**{**PAYMENT, "confidence": "medium"})
    assert not created and again.id == first.id and again.confidence == "medium"
    assert len(mem.recent()) == 1

    # The same symptoms long after are a new incident (a recurrence).
    mem.clock_state["t"] += DEDUP_S
    later, created = mem.save(**PAYMENT)
    assert created and later.id == "INC-002"


def test_resolve_and_reject(mem):
    mem.save(**PAYMENT)
    mem.save(**DB)
    inc = mem.resolve("INC-001", fix="Failed over to backup provider", root_cause="Gateway TLS cert expired")
    assert inc.status == "resolved" and inc.resolution == "Failed over to backup provider"
    assert inc.root_cause == "Gateway TLS cert expired"

    mem.reject("INC-002", reason="Load test, not an outage")
    assert [i.id for i, _ in mem.search(DB["symptoms"], k=5)] == ["INC-001"]  # rejected never comes back
    with pytest.raises(KeyError):
        mem.resolve("INC-999", fix="x")


def test_resolved_incident_does_not_absorb_a_recurrence(mem):
    mem.save(**PAYMENT)
    mem.resolve("INC-001", fix="Failed over")
    recurrence, created = mem.save(**PAYMENT)  # only *open* incidents are merged
    assert created and recurrence.id == "INC-002"


def test_memory_tools(mem):
    tb = Toolbox("http://prom", index=None, store=None, memory=mem)
    assert {"search_past_incidents", "save_postmortem"} <= {s["function"]["name"] for s in tb.specs()}

    assert tb.run("search_past_incidents", {"symptoms": "anything"}) == "No similar past incidents."
    assert tb.run("save_postmortem", PAYMENT) == "Saved as INC-001 (open until an engineer confirms the fix)."
    assert tb.run("save_postmortem", PAYMENT).startswith("Updated INC-001")

    mem.resolve("INC-001", fix="Failed over to backup provider")
    out = tb.run("search_past_incidents", {"symptoms": "payment-gateway errors, 5xx on /orders"})
    assert "INC-001" in out and "RESOLVED by an engineer. Fix that worked: Failed over to backup provider" in out


def test_toolbox_without_memory_hides_memory_tools():
    tb = Toolbox("http://prom", index=None, store=None)
    assert "save_postmortem" not in {s["function"]["name"] for s in tb.specs()}
    assert tb.run("save_postmortem", PAYMENT).startswith("error: unknown tool")


def test_clear_empties_records_and_search(mem):
    mem.save(**PAYMENT)
    mem.save(**DB)
    mem.clear()
    assert mem.recent() == [] and mem.search(PAYMENT["symptoms"]) == []
    assert mem.save(**DB)[0].id == "INC-001"  # numbering starts again
