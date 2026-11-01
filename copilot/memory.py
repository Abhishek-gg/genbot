"""Long-term incident memory: postmortems the agent writes and engineers confirm.

Each incident lives in SQLite (the record) and in a Chroma collection (for "have we seen
this before?" search by symptoms). The agent saves its diagnosis as an *open* incident.
An engineer then resolves it with the fix that actually worked, or rejects it if the
diagnosis was wrong, so bad diagnoses never come back as advice.
"""
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .vectorstore import connect

COLLECTION = "incidents"
DEDUP_S = 1800  # a save this soon after a similar open incident updates it instead of creating a new one
DEDUP_SIMILARITY = 0.8
MIN_SIMILARITY = 0.35  # weaker matches are noise, not "similar incidents"

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    n INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    title TEXT NOT NULL,
    symptoms TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    suggested_fix TEXT,
    confidence TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | resolved | rejected
    resolution TEXT                       -- what an engineer actually did, or why it was rejected
);
"""


@dataclass
class Incident:
    n: int
    created_at: float
    updated_at: float
    title: str
    symptoms: str
    root_cause: str
    suggested_fix: str | None
    confidence: str | None
    status: str
    resolution: str | None

    @property
    def id(self) -> str:
        return f"INC-{self.n:03d}"

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def document(self) -> str:
        """The text that gets embedded: what it looked like and what caused it."""
        return f"{self.title}. Symptoms: {self.symptoms}. Root cause: {self.root_cause}."


def parse_id(incident_id: str) -> int:
    return int(incident_id.upper().removeprefix("INC-"))


class IncidentMemory:
    def __init__(self, db_path: Path, chroma_location: str | Path, clock=time.time):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.col = connect(chroma_location).get_or_create_collection(
            COLLECTION, configuration={"hnsw": {"space": "cosine"}})
        self.clock = clock

    # --- records --------------------------------------------------------------

    def get(self, incident_id: str) -> Incident | None:
        row = self.db.execute("SELECT * FROM incidents WHERE n = ?", (parse_id(incident_id),)).fetchone()
        return Incident(**dict(row)) if row else None

    def recent(self, limit: int = 20) -> list[Incident]:
        rows = self.db.execute("SELECT * FROM incidents ORDER BY n DESC LIMIT ?", (limit,))
        return [Incident(**dict(r)) for r in rows]

    def _index(self, inc: Incident) -> None:
        self.col.upsert(ids=[inc.id], documents=[inc.document()],
                        metadatas=[{"status": inc.status, "created_at": inc.created_at}])

    def save(self, title: str, symptoms: str, root_cause: str, suggested_fix: str = "",
             confidence: str = "") -> tuple[Incident, bool]:
        """Record a diagnosis. Returns (incident, created). Re-diagnosing the same ongoing
        incident updates the open record instead of creating a duplicate."""
        now = self.clock()
        probe = f"{title}. Symptoms: {symptoms}. Root cause: {root_cause}."
        for inc, similarity in self.search(probe, k=1, statuses=("open",)):
            if now - inc.created_at < DEDUP_S and similarity >= DEDUP_SIMILARITY:
                with self.db:
                    self.db.execute(
                        "UPDATE incidents SET updated_at=?, title=?, symptoms=?, root_cause=?, suggested_fix=?, "
                        "confidence=? WHERE n=?",
                        (now, title, symptoms, root_cause, suggested_fix, confidence, inc.n))
                updated = self.get(inc.id)
                self._index(updated)
                return updated, False

        with self.db:
            cur = self.db.execute(
                "INSERT INTO incidents (created_at, updated_at, title, symptoms, root_cause, suggested_fix, confidence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, now, title, symptoms, root_cause, suggested_fix, confidence))
        inc = self.get(f"INC-{cur.lastrowid}")
        self._index(inc)
        return inc, True

    def _set_status(self, incident_id: str, status: str, resolution: str, root_cause: str | None) -> Incident:
        inc = self.get(incident_id)
        if inc is None:
            raise KeyError(f"no incident {incident_id}")
        with self.db:
            self.db.execute(
                "UPDATE incidents SET status=?, resolution=?, root_cause=COALESCE(?, root_cause), updated_at=? "
                "WHERE n=?", (status, resolution, root_cause, self.clock(), inc.n))
        inc = self.get(incident_id)
        self._index(inc)
        return inc

    def resolve(self, incident_id: str, fix: str, root_cause: str | None = None) -> Incident:
        """An engineer confirms the incident and records the fix that worked (optionally correcting the cause)."""
        return self._set_status(incident_id, "resolved", fix, root_cause)

    def reject(self, incident_id: str, reason: str) -> Incident:
        """The diagnosis was wrong: keep the record, but never offer it as a similar incident."""
        return self._set_status(incident_id, "rejected", reason, None)

    # --- search ---------------------------------------------------------------

    def search(self, text: str, k: int = 3, statuses=("open", "resolved")) -> list[tuple[Incident, float]]:
        if self.col.count() == 0:
            return []
        where = {"status": {"$in": list(statuses)}}
        res = self.col.query(query_texts=[text], n_results=k, where=where)
        hits = []
        for inc_id, dist in zip(res["ids"][0], res["distances"][0]):
            similarity = round(1 - dist, 2)
            if similarity >= MIN_SIMILARITY and (inc := self.get(inc_id)) is not None:
                hits.append((inc, similarity))
        return hits
