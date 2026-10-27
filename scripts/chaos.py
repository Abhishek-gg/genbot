"""Toggle faults in ShopAPI. Standard library only, so it runs without a venv.

    python scripts/chaos.py status
    python scripts/chaos.py set db_latency_s=2
    python scripts/chaos.py set error_rate=0.3 cpu_burn_ms=200
    python scripts/chaos.py set memory_leak_kb=512
    python scripts/chaos.py reset
"""
import json
import os
import sys
import urllib.request

BASE = os.getenv("SHOPAPI_URL", "http://localhost:8000")


def call(method: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}/chaos", data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main(argv: list[str]) -> None:
    if not argv or argv[0] == "status":
        result = call("GET")
    elif argv[0] == "reset":
        result = call("DELETE")
    elif argv[0] == "set" and len(argv) > 1:
        changes = {}
        for pair in argv[1:]:
            key, _, value = pair.partition("=")
            changes[key] = float(value)
        result = call("POST", changes)
    else:
        sys.exit(__doc__)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
