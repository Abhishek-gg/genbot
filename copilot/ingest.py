"""Tail ShopAPI logs into SQLite and index per-window summaries into Chroma.

    python -m copilot.ingest          # follow the log file forever
    python -m copilot.ingest --once   # ingest what's there, index every window, exit

Only logs/shopapi.log is read. logs/chaos_events.jsonl is evaluation ground truth and
must never reach the agent.
"""
import argparse
import time

from . import config
from .logstore import LogStore, parse_line
from .tailer import Tailer
from .vectorstore import WindowIndex
from .windows import summarize, window_start

SOURCE = "shopapi"
PRUNE_EVERY_S = 600
RECOVER_S = 300  # on startup, re-index this much recent history in case we crashed before indexing it


class Ingester:
    def __init__(self, store: LogStore, index: WindowIndex, tailer: Tailer, window_s: int):
        self.store, self.index, self.tailer, self.window_s = store, index, tailer, window_s
        self.dirty: set[float] = set()  # windows with new lines that still need (re)indexing

    def recover(self, full: bool = False) -> None:
        """Mark recent windows (or, with full=True, every stored window) for re-indexing."""
        last = self.store.max_ts()
        if last is None:
            return
        first = self.store.min_ts() if full else last - RECOVER_S
        self.dirty.update(
            window_start(ts, self.window_s) for ts in self.store.window_times(first, last + 1, self.window_s)
        )

    def poll(self) -> int:
        lines = self.tailer.read_new()
        if not lines:
            return 0
        rows = [r for r in map(parse_line, lines) if r]
        self.store.insert(rows, SOURCE, self.tailer.cp)
        self.dirty.update(window_start(r["ts"], self.window_s) for r in rows)
        return len(rows)

    def flush(self, now: float | None) -> int:
        """Index dirty windows that have closed. now=None indexes every dirty window, open or not."""
        ready = sorted(w for w in self.dirty if now is None or w + self.window_s + config.GRACE_S <= now)
        if not ready:
            return 0
        docs = [summarize(self.store.query(w, w + self.window_s), w, w + self.window_s) for w in ready]
        self.index.upsert(docs)
        self.dirty.difference_update(ready)
        return len(ready)

    def prune(self, now: float) -> None:
        cutoff = now - config.RETENTION_DAYS * 86400
        self.store.prune(cutoff)
        self.index.prune(cutoff)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="ingest the current file and exit")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between file checks")
    ap.add_argument("--reindex", action="store_true", help="rebuild every window in Chroma from SQLite first")
    args = ap.parse_args()

    store = LogStore(config.LOG_DB)
    index = WindowIndex(config.CHROMA)
    ing = Ingester(store, index, Tailer(config.LOG_FILE, store.checkpoint(SOURCE)), config.WINDOW_S)
    ing.recover(full=args.reindex)
    print(f"[ingest] {config.LOG_FILE} -> {config.LOG_DB} + Chroma at {config.CHROMA} "
          f"({config.WINDOW_S}s windows)", flush=True)

    if args.once:
        n = ing.poll()
        k = ing.flush(None)
        print(f"[ingest] {n} new lines, indexed {k} windows, {index.count()} windows total")
        return

    last_prune = 0.0
    try:
        while True:
            ing.poll()
            now = time.time()
            if k := ing.flush(now):
                print(f"[ingest] indexed {k} windows, {index.count()} total", flush=True)
            if now - last_prune > PRUNE_EVERY_S:
                ing.prune(now)
                last_prune = now
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("[ingest] stopped")


if __name__ == "__main__":
    main()
