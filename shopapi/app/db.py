"""A fake in-memory database with realistic, chaos-controllable latency."""
import asyncio
import logging
import random
import time

from . import chaos
from .metrics import DB_QUERY

log = logging.getLogger("shopapi.db")

SLOW_QUERY_THRESHOLD_S = 0.5

PRODUCTS = {
    i: {"id": i, "name": f"Product {i}", "price": round(random.uniform(99, 4999), 2), "stock": random.randint(0, 200)}
    for i in range(1, 51)
}
ORDERS: dict[int, dict] = {}


async def query(table: str, op: str) -> None:
    """Simulate a round trip to the database."""
    start = time.perf_counter()
    delay = random.uniform(0.003, 0.02)
    if chaos.state.db_latency_s:
        delay += chaos.state.db_latency_s * random.uniform(0.7, 1.3)
    await asyncio.sleep(delay)
    elapsed = time.perf_counter() - start

    DB_QUERY.labels(table, op).observe(elapsed)
    if elapsed > SLOW_QUERY_THRESHOLD_S:
        log.warning(
            "Slow query on %s.%s took %.0fms", table, op, elapsed * 1000,
            extra={"fields": {"table": table, "op": op, "duration_ms": round(elapsed * 1000)}},
        )
