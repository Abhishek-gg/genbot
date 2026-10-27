"""Steady synthetic traffic against ShopAPI so metrics and logs always have signal."""
import asyncio
import os
import random
import time
from collections import Counter

import httpx

TARGET = os.getenv("TARGET", "http://localhost:8000")
RPS = float(os.getenv("RPS", "5"))
TIMEOUT_S = 15

# (weight, method, path factory, body factory)
ACTIONS = [
    (50, "GET", lambda: "/products", None),
    (25, "GET", lambda: f"/products/{random.randint(1, 55)}", None),  # ids > 50 give realistic 404s
    (15, "POST", lambda: "/orders", lambda: {"product_id": random.randint(1, 50), "quantity": random.randint(1, 3)}),
    (10, "GET", lambda: f"/orders/{random.randint(1, 200)}", None),
]
WEIGHTS = [a[0] for a in ACTIONS]

stats: Counter = Counter()


async def fire(client: httpx.AsyncClient) -> None:
    _, method, path, body = random.choices(ACTIONS, weights=WEIGHTS)[0]
    try:
        r = await client.request(method, path(), json=body() if body else None)
        stats[r.status_code // 100 * 100] += 1
    except httpx.HTTPError:
        stats["conn_error"] += 1


async def report() -> None:
    while True:
        await asyncio.sleep(10)
        print(f"[loadgen] last 10s: {dict(stats)}", flush=True)
        stats.clear()


async def main() -> None:
    print(f"[loadgen] {RPS} rps -> {TARGET}", flush=True)
    async with httpx.AsyncClient(base_url=TARGET, timeout=TIMEOUT_S) as client:
        asyncio.create_task(report())
        interval = 1 / RPS
        while True:
            start = time.perf_counter()
            # Fire-and-forget so slow responses don't throttle the arrival rate.
            asyncio.create_task(fire(client))
            await asyncio.sleep(max(0, interval - (time.perf_counter() - start)))


if __name__ == "__main__":
    asyncio.run(main())
