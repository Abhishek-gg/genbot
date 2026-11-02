"""Public-demo watchdog: clear any injected fault that has been on longer than a time limit.

    python scripts/fault_ttl.py --ttl 600

Visitors to the hosted demo can break ShopAPI from the Chaos lab; this makes sure the next
visitor doesn't arrive to a service someone left broken. Standard library only.
"""
import argparse
import json
import os
import time
import urllib.request

BASE = os.getenv("SHOPAPI_URL", "http://localhost:8000")


def chaos(method: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(f"{BASE}/chaos", method=method), timeout=5) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ttl", type=int, default=600, help="seconds a fault may stay on")
    ap.add_argument("--poll", type=int, default=15)
    args = ap.parse_args()

    active_since: float | None = None
    while True:
        try:
            on = any(chaos("GET").values())
            if on and active_since is None:
                active_since = time.time()
            elif not on:
                active_since = None
            if active_since is not None and time.time() - active_since > args.ttl:
                chaos("DELETE")
                print(f"[fault-ttl] faults were on for over {args.ttl}s; reset", flush=True)
                active_since = None
        except OSError:
            pass  # ShopAPI not up yet
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
