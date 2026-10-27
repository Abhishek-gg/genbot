"""Fault injection.

Each fault produces realistic symptoms (slow queries, gateway timeouts, a growing
cache, CPU-heavy scoring) rather than announcing itself. Every change is also
recorded in LOG_DIR/chaos_events.jsonl as ground truth for evaluation; the agent
must never read that file.
"""
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

log = logging.getLogger("shopapi.chaos")


@dataclass
class ChaosState:
    db_latency_s: float = 0.0  # extra seconds added to every DB query
    error_rate: float = 0.0  # fraction of payment-gateway calls that time out
    memory_leak_kb: int = 0  # KB retained per order request, never freed
    cpu_burn_ms: int = 0  # busy-loop per product listing


state = ChaosState()
_leaked: list[bytes] = []


def leaked_bytes() -> int:
    return sum(len(b) for b in _leaked)


def _record(action: str) -> None:
    path = os.path.join(os.getenv("LOG_DIR", "logs"), "chaos_events.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "action": action, "state": asdict(state)}) + "\n")


def update(**changes) -> ChaosState:
    for key, value in changes.items():
        if not hasattr(state, key):
            raise ValueError(f"unknown chaos field: {key}")
        setattr(state, key, type(getattr(state, key))(value))
    _record("update")
    return state


def reset() -> ChaosState:
    global state
    state = ChaosState()
    _leaked.clear()
    _record("reset")
    return state


def leak() -> None:
    if state.memory_leak_kb > 0:
        # Non-zero fill: zeroed allocations are lazily mapped and wouldn't raise RSS.
        _leaked.append(b"x" * (state.memory_leak_kb * 1024))


def burn_cpu() -> None:
    if state.cpu_burn_ms > 0:
        end = time.perf_counter() + state.cpu_burn_ms / 1000
        while time.perf_counter() < end:
            pass
