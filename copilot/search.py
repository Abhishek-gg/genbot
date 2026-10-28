"""Search indexed logs from the command line, the same way the agent will.

    python -m copilot.search "payment gateway timeouts"
    python -m copilot.search "database is slow" --since 2h --issues
    python -m copilot.search "memory growing" --raw 5
"""
import argparse
import re
import time
from datetime import datetime, timezone

from . import config
from .logstore import LogStore
from .vectorstore import WindowIndex

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(value: str) -> float:
    """'30m', '2h', '1d' -> epoch seconds that long ago."""
    m = re.fullmatch(r"(\d+)([smhd])", value)
    if not m:
        raise argparse.ArgumentTypeError("use a duration like 30m, 2h or 1d")
    return time.time() - int(m[1]) * _UNITS[m[2]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--since", type=parse_since, help="only windows newer than this, e.g. 30m")
    ap.add_argument("--issues", action="store_true", help="only windows with warnings, errors or 5xx")
    ap.add_argument("-k", type=int, default=3, help="number of windows to return")
    ap.add_argument("--raw", type=int, default=0, metavar="N", help="also print N raw warning/error lines per window")
    args = ap.parse_args()

    index = WindowIndex(config.CHROMA_DIR)
    store = LogStore(config.LOG_DB) if args.raw else None
    hits = index.search(args.query, start=args.since, only_issues=args.issues, k=args.k)
    if not hits:
        print("No matching windows.")
    for h in hits:
        when = datetime.fromtimestamp(h["start"], tz=timezone.utc).strftime("%H:%M:%S")
        print(f"\n=== {h['id']} at {when} UTC (distance {h['distance']}) ===\n{h['document']}")
        if store:
            rows = [r for r in store.query(h["start"], h["end"]) if r["level"] in ("WARNING", "ERROR")]
            for r in rows[: args.raw]:
                print("  raw:", r["raw"][:200])


if __name__ == "__main__":
    main()
