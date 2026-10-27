# GenBot: Agentic Incident Copilot

An agentic RAG system that diagnoses production incidents by reasoning over **live metrics**
(Prometheus), **semantically indexed logs**, **deploy history** (GitHub) and **long-term memory
of past incidents**.

> Status: **Phase 1 of 7**. The demo service, fault injection, structured logs and metrics are done.

## How it works

```
ShopAPI (FastAPI) ──JSON logs──► log ingester ──► Chroma        (Phase 2)
       │ /metrics ─────────────► Prometheus
       ▼
 chaos switches                  Incident agent (LLM + tools)    (Phase 3)
 (latency, errors,               ├─ query_metrics   (PromQL, live)
  memory leak, CPU)              ├─ search_logs     (semantic + filters)
                                 ├─ get_recent_deploys (GitHub)
                                 └─ search/save past incidents (memory, Phase 4)
```

## Quick start

Needs Docker Desktop and Python 3.10+.

```bash
docker compose up -d --build
```

| URL | What |
|---|---|
| http://localhost:8000/docs | ShopAPI (Swagger UI) |
| http://localhost:8000/metrics | Raw Prometheus metrics |
| http://localhost:9090 | Prometheus UI (see the **Alerts** tab) |

The `loadgen` container sends about 5 requests per second, so there is always traffic. Logs go to `logs/shopapi.log`.

## Breaking things on purpose

```bash
python scripts/chaos.py set db_latency_s=2        # slow database → slow-query warnings, p95 alert
python scripts/chaos.py set error_rate=0.3        # payment gateway timeouts → 502s, error-rate alert
python scripts/chaos.py set memory_leak_kb=512    # order cache leak → memory alert
python scripts/chaos.py set cpu_burn_ms=300       # CPU-heavy product listing → CPU alert
python scripts/chaos.py status
python scripts/chaos.py reset
```

Each fault shows realistic symptoms in metrics and logs and never names itself. The true cause
is written to `logs/chaos_events.jsonl`, which serves as **ground truth for evaluation**. The agent
must never read this file.

## Metrics exposed

| Metric | Labels |
|---|---|
| `http_requests_total` | method, route, status |
| `http_request_duration_seconds` | method, route |
| `db_query_duration_seconds` | table, op |
| `dependency_errors_total` | dependency |
| `order_cache_bytes` | |
| `shopapi_build_info` | version |
| `process_*` (CPU, memory) | |

## Roadmap

1. ✅ Demo service, chaos switches, logs, metrics, Prometheus alerts
2. Log ingester → Chroma (incremental, time-windowed chunks)
3. Agent loop with tools (Groq / Gemini free tier, swappable)
4. Incident memory (postmortems, "this looks like INC-004")
5. Alertmanager webhook → auto-investigation → Discord
6. Evaluation harness: inject N faults and score root-cause accuracy and time to diagnosis
7. Streamlit UI showing the agent's reasoning steps, plus deployment
