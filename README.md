# GenBot: Agentic Incident Copilot

An agentic RAG system that diagnoses production incidents by reasoning over **live metrics**
(Prometheus), **semantically indexed logs**, **deploy history** (GitHub) and **long-term memory
of past incidents**.

> Status: **Phase 6 of 7**. The demo service, log ingester, incident agent, incident memory, alert-driven
> investigation and evaluation harness are built. In the evaluation so far GenBot diagnosed 9 of 10 injected
> scenarios correctly (see [Evaluation](#evaluation)), and the agent has
> correctly diagnosed each of the five live scenarios (see [First live results](#first-live-results)).

## How it works

```
ShopAPI (FastAPI) ──JSON logs──► log ingester ──► SQLite (raw lines)
       │                                     └─► Chroma server (window summaries)
       │ /metrics ─────────────► Prometheus ──alerts──► Alertmanager ──webhook──► responder ──► Discord
       ▼                                                                           │ (runs the agent)
 chaos switches                  Incident agent (LLM + tools)
 (latency, errors,               ├─ service_health  (key metrics + alerts, live)
  memory leak, CPU)              ├─ query_metrics   (custom PromQL, live)
                                 ├─ search_logs     (semantic, Chroma)
                                 ├─ get_log_lines   (exact, SQLite)
                                 ├─ get_recent_deploys (versions + GitHub)
                                 ├─ search_past_incidents (memory, Chroma)
                                 └─ save_postmortem       (memory, SQLite + Chroma)
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
| http://localhost:8001 | Chroma vector DB server (used by the ingester and the agent) |
| http://localhost:9093 | Alertmanager (groups alerts and calls the responder) |

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
- **Chroma runs as a server** (in Docker Compose) because the ingester writes while the agent reads.
  Chroma's embedded on-disk mode is not safe across processes: the reader's index goes stale and
  queries fail with "Error finding id". Set `CHROMA_URL` to a folder path to use embedded mode in a single process.
- **Crash-safe:** each batch of lines and the file checkpoint commit in one transaction. On restart it
  re-indexes the last 5 minutes, and windows are upserted by start time, so nothing is duplicated.
- **Handles rotation:** when the log rotates, the tailer drains the rest of `shopapi.log.1` before switching files.
- **Retention:** 7 days (`COPILOT_RETENTION_DAYS`).

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip

.venv/Scripts/python -m copilot.ingest               # follow the log (Ctrl+C to stop)
.venv/Scripts/python -m copilot.ingest --once        # or: ingest what's there and exit
.venv/Scripts/python -m copilot.ingest --reindex     # rebuild every window from SQLite (e.g. new Chroma volume)

.venv/Scripts/python -m copilot.search "payment provider failing" --since 30m --issues
.venv/Scripts/python -m copilot.search "database is slow" --raw 3   # also show raw lines
.venv/Scripts/python -m pytest
```

## Asking the agent

The agent is a hand-written tool-calling loop ([copilot/agent.py](copilot/agent.py)): the LLM picks a tool, the
tool runs, its result is fed back, and this repeats until the model answers (at most 8 tool calls). No framework
is used, so every step can be inspected.

| Tool | Source | Returns |
|---|---|---|
| `service_health` | Prometheus | Traffic, 5xx ratio, p95 per route, DB time, dependency errors, CPU, memory and alerts, sorted into *abnormal now* (in order of onset), *abnormal earlier but recovered*, and *normal* |
| `query_metrics` | Prometheus | Any PromQL range query, summarized the same way |
| `search_logs` | Chroma | The most relevant 30-second log summaries |
| `get_log_lines` | SQLite | Exact lines filtered by level, route, text or trace ID |
| `get_recent_deploys` | Prometheus + GitHub | Which versions ran and when, plus recent commits |
| `search_past_incidents` | Incident memory | Similar past incidents; resolved ones include the fix that worked |
| `save_postmortem` | Incident memory | Saves this diagnosis as an open incident (`INC-007`) |

Tools return compact text rather than raw data, because free LLM tiers allow only about 8K tokens per minute.
Errors such as a bad PromQL query come back as text so the model can correct itself.

**LLM (free):** Groq is the default (`openai/gpt-oss-120b`). Gemini and a local Ollama model also work. All three
use the OpenAI-compatible API, so one small client ([copilot/llm.py](copilot/llm.py)) covers them. It handles
free-tier limits in two ways: per-minute limits are retried using the server's `Retry-After` header, and an exhausted
**daily** quota switches straight to a fallback model (`openai/gpt-oss-20b`), because free quotas are counted per
model and waiting hours for a reset is pointless.

```bash
cp .env.example .env          # then paste a free key from https://console.groq.com/keys
.venv/Scripts/python -m copilot.ask "Why are checkouts failing?"
.venv/Scripts/python -m copilot.ask "Is anything wrong right now?" -v        # show every tool's output
.venv/Scripts/python -m copilot.ask "..." --provider gemini
```

Keep `python -m copilot.ingest` running in another terminal so recent logs are searchable.

### First live results

Each fault was injected on its own, a few minutes after the previous one was reset, and the agent got the same neutral
question ("Something is wrong with ShopAPI right now. What's causing it?") with no hint about the fault.

| Scenario | Diagnosis | Tool calls | Time |
|---|---|---|---|
| Healthy (no fault) | ✅ "ShopAPI is currently healthy" | 1 | 2s |
| Slow database | ✅ DB queries ~100× slower, all routes affected | 6 | 60s |
| Payment gateway errors | ✅ Payment-gateway timeouts on `/orders` | 3 | 5s |
| Memory leak | ✅ Order cache growing without bound | 4–5 | 16–29s |
| CPU saturation | ✅ CPU saturation, DB/route latency as effects | 4 | 8s |

These are single runs, not a benchmark; Phase 6 measures accuracy over many runs. Getting here took two
design fixes, both found by running against live data:

- **Past incidents were blamed for current ones.** The first version of `service_health` reported "max X at T"
  over the whole window, so a recovered fault from 20 minutes earlier looked as bad as the current one (1 of 4
  correct). It now separates *abnormal now* from *recovered*, judging "now" by the last 2 minutes.
- **Log searches reached back into the previous incident.** A CPU fault leaves no log messages, so the agent
  found the previous incident's warnings and blamed them. `service_health` now lists current signals in the order
  they started and tells the agent the `minutes` value that covers only this incident.

## Incident memory

After diagnosing a current problem, the agent saves a postmortem: title, symptoms with numbers, root cause and a
suggested fix. When the next incident starts, it searches those postmortems by symptoms, so it can say *"this
matches INC-001, which was fixed by failing over to the backup payment provider"*.

An LLM's diagnosis is not ground truth, so memory keeps **a human in the loop**:

| Status | Meaning | Offered to the agent as |
|---|---|---|
| `open` | Saved by the agent, not yet reviewed | An unconfirmed lead |
| `resolved` | An engineer confirmed it and recorded the fix that actually worked | A proven fix |
| `rejected` | The diagnosis was wrong | Never shown again |

Re-diagnosing the *same ongoing* incident (a similar open incident from the last 30 minutes) updates it instead of
creating a duplicate. The same symptoms after it was resolved count as a recurrence and get a new ID.

```bash
.venv/Scripts/python -m copilot.incidents list
.venv/Scripts/python -m copilot.incidents show INC-001
.venv/Scripts/python -m copilot.incidents resolve INC-001 --fix "Failed over to the backup payment provider"
.venv/Scripts/python -m copilot.incidents reject INC-002 --reason "Was a load test, not an outage"
```

### Live check

| Step | What GenBot did |
|---|---|
| Payment fault, first time | Found no past incidents, diagnosed the gateway timeouts, saved **INC-001** |
| Engineer resolves INC-001 | `--fix "Failed over to the backup payment provider"` |
| Same fault recurs | Matched INC-001 (similarity 0.78), recommended its proven fix first, saved the recurrence as **INC-002** |
| A different fault (slow DB) | No false match ("No similar past incidents"), saved **INC-003** |
| Healthy | Saved nothing |

## Automatic investigation (alerts → Discord)

GenBot doesn't wait to be asked. When a Prometheus alert fires:

1. **Alertmanager** groups related alerts (30 s `group_wait`) and calls the responder's webhook.
2. **The responder** ([copilot/responder.py](copilot/responder.py)) turns the alerts into a question and runs the agent
   in a background worker, one investigation at a time. Repeats of the same alerts within 15 minutes are skipped;
   a *new* alert in the same group triggers a fresh investigation.
3. **The diagnosis is posted to Discord** as an embed titled with the alerts and the saved incident ID
   (e.g. `🚨 HighErrorRate · INC-004`). When the alerts clear, a `✅ Resolved` message follows, with a reminder to
   record the fix. If the investigation fails (e.g. the LLM is unreachable), that is posted too, instead of silence.

Without `DISCORD_WEBHOOK_URL` in `.env`, messages are printed to the console instead.

```bash
.venv/Scripts/python -m copilot.ingest       # terminal 1: keep logs searchable
.venv/Scripts/python -m copilot.responder    # terminal 2: listens on :8090 for Alertmanager
python scripts/chaos.py set error_rate=0.5   # about 2 minutes later, GenBot posts its diagnosis
```

**Checklist guardrail.** On the smaller fallback model, the agent diagnosed the fault correctly but skipped the memory
steps and then claimed there was "no matching past incident" without having searched. Prompts alone aren't
enough, so the loop enforces it: if `service_health` reports a current incident and the model tries to answer
without having searched memory and saved a postmortem, it is sent back once to do so.

## Evaluation

`python -m copilot.evaluate` measures GenBot instead of trusting single demos. Each trial resets every fault,
lets the service settle, injects **one** fault, waits two minutes for symptoms, asks a neutral question
("Something is wrong with ShopAPI right now. What's causing it?") and grades the answer's **Root cause** line:

- **Grading** is a transparent keyword classifier ([copilot/evaluate.py](copilot/evaluate.py)): the cause category
  mentioned *earliest* in the root-cause line wins, so "memory leak leading to CPU saturation" counts as *memory*.
  It is unit-tested against real answers from live runs, including wrong ones.
- **Each trial is independent**: the model is pinned (no fallback), incident memory is a separate, emptied store,
  and scenario order is shuffled within each round.
- Results are written to [eval/results/](eval/results/) as JSONL (full answers) plus a Markdown report.

```bash
.venv/Scripts/python -m copilot.ingest                                   # terminal 1
.venv/Scripts/python -m copilot.evaluate --trials 2                      # terminal 2 (stop the responder first)
.venv/Scripts/python -m copilot.evaluate --trials 2 --model openai/gpt-oss-20b
```

### Results so far

| Model | Correct | Median time to diagnosis | Mean tool calls | Mean tokens |
|---|---|---|---|---|
| `gpt-oss-20b` | **7/8 (88%)**: 2/2 payment, 2/2 database, 1/1 memory, 1/1 CPU, 1/2 healthy | 137 s | 7.1 | 25,261 |
| `gpt-oss-120b` | 2/2 before hitting its daily quota (payment, memory) | 63 s | 6.5 | 18,576 |

Both runs stopped early on Groq's free daily quota of 200K tokens per model (one CPU trial each did not run).
The full `gpt-oss-120b` run is pending.

**The one misdiagnosis was a real tool bug, found by the evaluation.** A *healthy* trial that ran right after a
database trial was diagnosed as "slow database queries". Replaying `service_health` against the real Prometheus
history for that moment showed why: latency had been back to normal for a full minute, but the tool judged "now" by
the average of the last 2 minutes, which still included the tail of the previous fault, so it reported the database
as **ABNORMAL NOW**. The fix: a signal only counts as abnormal now if its last minute is abnormal too; otherwise it is
listed as recovered. The same replay now reports "ABNORMAL NOW: nothing", while replaying during the actual
database fault still reports it. The evaluation's settle time also went up from 120 s to 210 s.

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
3. ✅ Agent loop with tools (Groq / Gemini / Ollama, swappable)
4. ✅ Incident memory (postmortems with human review, "this matches INC-001")
5. ✅ Alertmanager webhook → auto-investigation → Discord, with model fallback and a memory guardrail
6. ✅ Evaluation harness: inject N faults and score root-cause accuracy and time to diagnosis (full 120b run pending)
7. Streamlit UI showing the agent's reasoning steps, plus deployment
