# GenBot: Agentic Incident Copilot

An agentic RAG system that diagnoses production incidents by reasoning over **live metrics**
(Prometheus), **semantically indexed logs**, **deploy history** (GitHub) and **long-term memory
of past incidents**.

> Status: **Phase 2 of 7**. The demo service, fault injection, metrics and the log ingester are done.

## How it works

```
ShopAPI (FastAPI) ──JSON logs──► log ingester ──► SQLite (raw lines)
       │                                     └─► Chroma (window summaries)
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

## Log ingestion and search

The ingester tails `logs/shopapi.log`. It stores every line in SQLite for exact lookups and indexes
one **summary per 30-second window** in Chroma for semantic search. Embedding each request line would bury
the index in thousands of near-identical `GET /products 200` entries. A window summary
holds traffic stats, latency percentiles, the slowest routes and the distinct warnings and errors, with numbers
and IDs templated out so that repeats collapse (`ERROR x15: Payment failed: Timeout ... after <N>ms`).

- **Embeddings:** all-MiniLM-L6-v2 (ONNX, via Chroma). Runs locally and needs no API key or PyTorch.
- **Crash-safe:** each batch of lines and the file checkpoint commit in one transaction. On restart it
  re-indexes the last 5 minutes, and windows are upserted by start time, so nothing is duplicated.
- **Handles rotation:** when the log rotates, the tailer drains the rest of `shopapi.log.1` before switching files.
- **Retention:** 7 days (`COPILOT_RETENTION_DAYS`).

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip

.venv/Scripts/python -m copilot.ingest               # follow the log (Ctrl+C to stop)
.venv/Scripts/python -m copilot.ingest --once        # or: ingest what's there and exit

.venv/Scripts/python -m copilot.search "payment provider failing" --since 30m --issues
.venv/Scripts/python -m copilot.search "database is slow" --raw 3   # also show raw lines
.venv/Scripts/python -m pytest
```

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
2. ✅ Log ingester → SQLite + Chroma (incremental, time-windowed summaries)
3. Agent loop with tools (Groq / Gemini free tier, swappable)
4. Incident memory (postmortems, "this looks like INC-004")
5. Alertmanager webhook → auto-investigation → Discord
6. Evaluation harness: inject N faults and score root-cause accuracy and time to diagnosis
7. Streamlit UI showing the agent's reasoning steps, plus deployment
