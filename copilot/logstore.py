"""SQLite store for raw log lines: exact lookups by time, level, route or trace ID.

The vector index holds per-window summaries for semantic search; this store holds
the underlying lines so the agent can drill down into the evidence.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .tailer import Checkpoint

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    level TEXT,
    version TEXT,
    msg TEXT,
    trace_id TEXT,
    route TEXT,
    status INTEGER,
    duration_ms INTEGER,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS logs_trace ON logs(trace_id);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""

COLUMNS = ("ts", "level", "version", "msg", "trace_id", "route", "status", "duration_ms")


def parse_line(line: str) -> dict | None:
    """Parse one JSON log line into a row dict, or None if it isn't valid."""
    try:
        entry = json.loads(line)
        ts = datetime.fromisoformat(entry["ts"]).timestamp()
    except (ValueError, KeyError, TypeError):
        return None
    row = {col: entry.get(col) for col in COLUMNS}
    row["ts"] = ts
    row["raw"] = line
    # Keep exception details for errors so the agent sees what failed.
    if "exc_type" in entry:
        row["msg"] = f"{row['msg']} [{entry['exc_type']}]"
    return row


class LogStore:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def checkpoint(self, source: str) -> Checkpoint:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (f"checkpoint:{source}",)).fetchone()
        return Checkpoint(**json.loads(row["value"])) if row else Checkpoint()

    def insert(self, rows: list[dict], source: str, cp: Checkpoint) -> None:
        """Insert rows and advance the checkpoint in one transaction, so a crash never duplicates or skips lines."""
        with self.db:
            self.db.executemany(
                f"INSERT INTO logs ({', '.join(COLUMNS)}, raw) VALUES ({', '.join('?' * (len(COLUMNS) + 1))})",
                [tuple(r[c] for c in COLUMNS) + (r["raw"],) for r in rows],
            )
            self.db.execute(
                "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                (f"checkpoint:{source}", json.dumps({"offset": cp.offset, "inode": cp.inode})),
            )

    def query(
        self,
        start: float,
        end: float,
        level: str | None = None,
        route: str | None = None,
        trace_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM logs WHERE ts >= ? AND ts < ?"
        args: list = [start, end]
        if level:
            sql += " AND level = ?"
            args.append(level.upper())
        if route:
            sql += " AND route = ?"
            args.append(route)
        if trace_id:
            sql += " AND trace_id = ?"
            args.append(trace_id)
        sql += " ORDER BY ts"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    def max_ts(self) -> float | None:
        return self.db.execute("SELECT MAX(ts) FROM logs").fetchone()[0]

    def prune(self, before: float) -> int:
        with self.db:
            return self.db.execute("DELETE FROM logs WHERE ts < ?", (before,)).rowcount
