"""The grader, checked against real Root cause lines GenBot produced during live runs (including wrong ones)."""
import pytest

from copilot.evaluate import Trial, grade, report, root_cause_line

REAL_ANSWERS = [
    ("**Root cause**: Payment‑gateway timeouts (high confidence).", "payment"),
    ("**Root cause**: Payment‑gateway dependency failure (timeouts) – confidence **high**.", "payment"),
    ("**Root cause**: External dependency failure – the payment‑gateway is timing out. **Confidence**: high.",
     "payment"),
    ("**Root cause**: Database slowdown (high query latency) – confidence **high**.", "database"),
    ("**Root cause**: Database query latency spike (high confidence).", "database"),
    ("**Root cause**: Unbounded order‑cache growth (memory leak) – **high confidence**.", "memory"),
    ("**Root cause**: cache memory growth leading to memory pressure (medium confidence).", "memory"),
    # The wrong CPU diagnosis from the third live run: blames the earlier memory leak.
    ("**Root cause**: Unbounded order‑cache growth (memory leak) leading to CPU saturation and slowed DB "
     "queries – **high confidence**.", "memory"),
    ("**Root cause**: CPU (cores) usage spiking to ~0.52 × core (normal ≈ 0.018) since 08:22:16 UTC, "
     "causing downstream latency; confidence **high**.", "cpu"),
    ("**Root cause**: N/A (no current issue detected).", "healthy"),
    ("**Root cause**: none (high confidence).", "healthy"),
]


@pytest.mark.parametrize("answer,expected", REAL_ANSWERS)
def test_grades_real_answers(answer, expected):
    full = f"**Summary**: something.\n{answer}\n**Evidence**:\n- payment errors earlier, CPU normal"
    assert grade(full) == expected


def test_healthy_verdict_that_mentions_a_past_cause():
    assert grade("**Root cause**: none – the earlier payment-gateway issue has recovered.") == "healthy"


def test_missing_or_vague_root_cause():
    assert grade("I could not determine anything.") == "unknown"
    assert grade("**Root cause**: something odd happened (low confidence).") == "unknown"
    assert root_cause_line("**Root cause** Payment gateway\n**Evidence**: x") == "Payment gateway"


def trial(scenario, predicted, seconds=10.0, tools=("service_health", "save_postmortem"), error=None):
    expected = "healthy" if scenario == "healthy" else scenario
    return Trial(scenario, expected, predicted, predicted == expected and not error, "m", seconds, len(tools), 3,
                 1000, list(tools), f"rc for {predicted}", "answer", error)


def test_report_summarizes_accuracy_and_lists_mistakes():
    md = report([trial("payment", "payment", 5), trial("payment", "payment", 7), trial("cpu", "memory", 80),
                 trial("healthy", "healthy", 2, tools=("service_health",)),
                 trial("database", "error", error="quota exhausted")], "test-model", "2026-11-01 13:00")
    assert "| payment | 2/2 | 100% | 6s | 2 |" in md
    assert "| cpu | 0/1 | 0% |" in md
    assert "**Overall: 3/4 correct (75%).**" in md
    assert "Saved a postmortem in 3/3 fault trials." in md
    assert "- **cpu** graded as *memory*: rc for memory" in md
    assert "- database: quota exhausted" in md
