"""GenBot web UI.

    streamlit run copilot/ui.py

Tabs: ask GenBot (with every tool call visible), live service health, incident review
(resolve or reject), the feed of automatic investigations, and a chaos lab for demos.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # `streamlit run` doesn't add the repo root
from copilot import config  # noqa: E402
from copilot.ask import build_agent  # noqa: E402
from copilot.evaluate import RESULTS_DIR  # noqa: E402
from copilot.llm import PROVIDERS, LLMError  # noqa: E402
from copilot.memory import IncidentMemory  # noqa: E402
from copilot.tools import Toolbox  # noqa: E402

SHOPAPI = "http://localhost:8000"
TOOL_ICONS = {"service_health": "📈", "query_metrics": "📊", "search_logs": "🔎", "get_log_lines": "📜",
              "get_recent_deploys": "🚀", "search_past_incidents": "🧠", "save_postmortem": "💾"}
CHARTS = [
    ("5xx error ratio", 'sum(rate(http_requests_total{status=~"5.."}[1m])) / sum(rate(http_requests_total[1m]))', None),
    ("p95 latency by route (s)", "histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket[1m])))", "route"),
    ("CPU (cores)", 'rate(process_cpu_seconds_total{job="shopapi"}[1m])', None),
    ("Memory RSS (MB)", 'process_resident_memory_bytes{job="shopapi"} / 1e6', None),
]
FAULTS = {
    "Slow database": {"db_latency_s": 1.5},
    "Payment gateway errors": {"error_rate": 0.5},
    "Memory leak": {"memory_leak_kb": 8192},
    "CPU saturation": {"cpu_burn_ms": 250},
}

st.set_page_config(page_title="GenBot", page_icon="🤖", layout="wide")


@st.cache_resource
def memory() -> IncidentMemory:
    return IncidentMemory(config.INCIDENT_DB, config.CHROMA)


# Streamlit reruns the whole script on every click, including tabs that aren't open. Caching the
# Prometheus calls for a few seconds keeps reruns fast, so clicks aren't lost while the page is busy.
@st.cache_data(ttl=15, show_spinner=False)
def health_summary(minutes: int) -> str:
    return Toolbox(config.PROMETHEUS_URL, None, None).run("service_health", {"minutes": minutes})


@st.cache_data(ttl=15, show_spinner=False)
def prom_range(promql: str, minutes: int) -> list[dict]:
    end = time.time()
    r = httpx.get(f"{config.PROMETHEUS_URL}/api/v1/query_range",
                  params={"query": promql, "start": end - minutes * 60, "end": end, "step": 15}, timeout=10)
    return r.json()["data"]["result"]


def chaos(method: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{SHOPAPI}/chaos", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


# --- Sidebar --------------------------------------------------------------------

with st.sidebar:
    st.title("🤖 GenBot")
    st.caption("Agentic incident copilot for ShopAPI: live metrics, logs, deploys and incident memory.")
    locked = bool(config.DEMO_MAX_QUESTIONS)  # public demo: visitors can't switch to another provider or model
    provider = st.selectbox("LLM provider", list(PROVIDERS), index=list(PROVIDERS).index(config.LLM_PROVIDER),
                            disabled=locked)
    default_model = config.LLM_MODEL if provider == config.LLM_PROVIDER and config.LLM_MODEL else PROVIDERS[provider][2]
    model = st.text_input("Model", default_model, disabled=locked)
    st.divider()
    try:
        state = chaos("GET")
        active = {k: v for k, v in state.items() if v}
        st.markdown("**ShopAPI:** 🟢 up" + (f"  \n⚠️ faults on: `{active}`" if active else ""))
    except OSError:
        st.markdown("**ShopAPI:** 🔴 unreachable. Start it with `docker compose up -d`.")

ask_tab, health_tab, incidents_tab, feed_tab, lab_tab, eval_tab = st.tabs(
    ["💬 Ask", "📈 Service health", "🧠 Incidents", "🚨 Alert feed", "🧪 Chaos lab", "📏 Evaluation"])

# --- Ask ------------------------------------------------------------------------

with ask_tab:
    st.subheader("Ask GenBot")
    examples = ["Something is wrong with ShopAPI right now. What's causing it?",
                "Is anything wrong with ShopAPI right now?", "Why are checkouts failing?"]
    question = st.text_input("Question", examples[0])
    st.caption("Examples: " + " · ".join(f"*{e}*" for e in examples[1:]))

    asked = st.session_state.get("questions_asked", 0)
    limit = config.DEMO_MAX_QUESTIONS
    if limit:
        st.caption(f"Public demo: {max(limit - asked, 0)} of {limit} questions left in this session "
                   "(each investigation uses the free LLM quota).")
    if st.button("Investigate", type="primary", disabled=bool(limit) and asked >= limit):
        st.session_state["questions_asked"] = asked + 1
        status = st.status("Investigating...", expanded=True)

        def show_step(step):
            icon = TOOL_ICONS.get(step.tool, "🔧")
            args = ", ".join(f"{k}={v!r}" for k, v in step.args.items())
            with status:
                with st.expander(f"{icon} `{step.tool}({args})` · {step.seconds:.1f}s"):
                    st.code(step.result, language=None)

        try:
            result = build_agent(provider, model, on_step=show_step).ask(question)
        except LLMError as e:
            status.update(label="LLM error", state="error")
            st.error(str(e))
        else:
            status.update(label=f"Done: {len(result.steps)} tool calls, {result.llm_calls} LLM calls, "
                                f"{result.tokens:,} tokens, {result.seconds:.0f}s on {result.model}",
                          state="complete", expanded=False)
            st.markdown(result.answer)

# --- Service health ---------------------------------------------------------------

with health_tab:
    left, right = st.columns([1, 3])
    minutes = left.select_slider("Window", options=[5, 15, 30, 60], value=15, format_func=lambda m: f"{m} min")
    right.caption("The same view GenBot's `service_health` tool gives the agent, plus the raw series "
                  "(times in UTC, refreshed every 15 s).")
    try:
        summary = health_summary(minutes)
        if "ABNORMAL NOW: nothing" in summary:
            st.success("Nothing is abnormal right now.")
        else:
            st.error("Something is abnormal right now.")
        st.code(summary, language=None)
        cols = st.columns(2)
        for i, (title, promql, label) in enumerate(CHARTS):
            series = {}
            for s in prom_range(promql, minutes):
                name = s["metric"].get(label, title) if label else title
                series[name] = pd.Series({pd.to_datetime(float(t), unit="s"): float(v) for t, v in s["values"]})
            with cols[i % 2]:
                st.markdown(f"**{title}**")
                if series:
                    st.line_chart(pd.DataFrame(series), height=220)
                else:
                    st.caption("no data")
    except (httpx.HTTPError, KeyError) as e:
        st.warning(f"Prometheus is not reachable at {config.PROMETHEUS_URL}: {e}")

# --- Incidents ----------------------------------------------------------------------

with incidents_tab:
    st.subheader("Incident memory")
    st.caption("GenBot saves each diagnosis as **open**. Resolve it with the fix that actually worked, or reject a "
               "wrong diagnosis. Only resolved incidents are offered to the agent as proven fixes.")
    try:
        incidents = memory().recent(50)
    except Exception as e:  # Chroma down, etc.
        st.warning(f"Incident memory unavailable: {e}")
        incidents = []
    if not incidents:
        st.info("No incidents yet.")
    else:
        st.dataframe(pd.DataFrame([{"ID": i.id, "Status": i.status, "Created": i.date, "Title": i.title,
                                    "Confidence": i.confidence} for i in incidents]),
                     hide_index=True, use_container_width=True)
        chosen = st.selectbox("Review incident", [i.id for i in incidents])
        inc = memory().get(chosen)
        st.markdown(f"#### {inc.id}: {inc.title}  \n**Status:** {inc.status} · **Confidence:** {inc.confidence or '-'}")
        st.markdown(f"**Symptoms:** {inc.symptoms}  \n**Root cause:** {inc.root_cause}  \n"
                    f"**Suggested fix:** {inc.suggested_fix or '-'}")
        if inc.resolution:
            st.markdown(f"**{'Fix that worked' if inc.status == 'resolved' else 'Rejected because'}:** {inc.resolution}")
        resolve_col, reject_col = st.columns(2)
        with resolve_col.form(f"resolve-{inc.id}"):
            fix = st.text_input("Fix that worked")
            cause = st.text_input("Corrected root cause (optional)")
            if st.form_submit_button("Resolve") and fix:
                memory().resolve(inc.id, fix, cause or None)
                st.rerun()
        with reject_col.form(f"reject-{inc.id}"):
            reason = st.text_input("Why the diagnosis was wrong")
            if st.form_submit_button("Reject") and reason:
                memory().reject(inc.id, reason)
                st.rerun()

# --- Alert feed ---------------------------------------------------------------------

with feed_tab:
    st.subheader("Automatic investigations")
    st.caption("Written by `python -m copilot.responder` whenever Alertmanager fires an alert.")
    log = Path(config.INVESTIGATIONS_LOG)
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    if not entries:
        st.info("No automatic investigations yet. Start the responder, then inject a fault in the Chaos lab.")
    for e in reversed(entries[-20:]):
        when = datetime.fromisoformat(e["at"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        title = f"{'⚠️' if e.get('error') else '🚨'} {', '.join(e['alerts'])} · {when}"
        if e.get("incident"):
            title += f" · {e['incident']}"
        with st.expander(title):
            if e.get("error"):
                st.error(e["error"])
            else:
                st.caption(f"{len(e.get('steps', []))} tool calls · {e.get('seconds', 0):.0f}s · {e.get('model', '')}")
                st.markdown(e.get("answer", ""))

# --- Chaos lab ------------------------------------------------------------------------

with lab_tab:
    st.subheader("Chaos lab")
    st.caption("Break ShopAPI on purpose, wait about two minutes for symptoms (and alerts), then ask GenBot. "
               "Faults show only symptoms; they never name themselves.")
    cols = st.columns(len(FAULTS) + 1)
    for col, (label, settings) in zip(cols, FAULTS.items()):
        if col.button(label, use_container_width=True):
            chaos("POST", settings)
            st.toast(f"Injected: {label}")
            st.rerun()
    if cols[-1].button("Reset all", type="primary", use_container_width=True):
        chaos("DELETE")
        st.toast("All faults cleared")
        st.rerun()
    try:
        st.json(chaos("GET"))
    except OSError:
        st.warning("ShopAPI is not reachable.")

# --- Evaluation ------------------------------------------------------------------------

with eval_tab:
    st.subheader("Evaluation reports")
    reports = sorted(RESULTS_DIR.glob("*.md"), reverse=True) if RESULTS_DIR.exists() else []
    if not reports:
        st.info("No evaluation runs yet: `python -m copilot.evaluate --trials 2`.")
    else:
        chosen = st.selectbox("Report", reports, format_func=lambda p: p.stem)
        st.markdown(chosen.read_text(encoding="utf-8"))
