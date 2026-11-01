"""Chroma index of log-window summaries, embedded locally with all-MiniLM-L6-v2 (ONNX)."""
from pathlib import Path
from urllib.parse import urlparse

import chromadb

COLLECTION = "log_windows"
BATCH = 256


def connect(location: str | Path):
    """An http(s) URL connects to a Chroma server; anything else is a folder for embedded mode,
    which is only safe when a single process uses it (tests, one-off scripts)."""
    loc = str(location)
    if loc.startswith(("http://", "https://")):
        u = urlparse(loc)
        return chromadb.HttpClient(host=u.hostname, port=u.port or 8000, ssl=u.scheme == "https")
    return chromadb.PersistentClient(path=loc)


class WindowIndex:
    def __init__(self, location: str | Path):
        client = connect(location)
        self.col = client.get_or_create_collection(COLLECTION, configuration={"hnsw": {"space": "cosine"}})

    def upsert(self, windows: list[tuple[str, dict]]) -> None:
        """windows: (document, metadata) pairs. A window's ID is its start time, so re-indexing replaces it."""
        for i in range(0, len(windows), BATCH):
            batch = windows[i : i + BATCH]
            self.col.upsert(
                ids=[f"w{int(meta['start'])}" for _, meta in batch],
                documents=[doc for doc, _ in batch],
                metadatas=[meta for _, meta in batch],
            )

    def search(
        self,
        query: str,
        start: float | None = None,
        end: float | None = None,
        only_issues: bool = False,
        k: int = 5,
    ) -> list[dict]:
        """Semantic search over windows overlapping [start, end]."""
        filters: list[dict] = []
        if start is not None:
            filters.append({"end": {"$gt": start}})
        if end is not None:
            filters.append({"start": {"$lt": end}})
        if only_issues:
            filters.append({"has_issues": True})
        where = filters[0] if len(filters) == 1 else ({"$and": filters} if filters else None)

        res = self.col.query(query_texts=[query], n_results=k, where=where)
        return [
            {"id": i, "document": d, "distance": round(dist, 3), **m}
            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]

    def prune(self, before: float) -> None:
        self.col.delete(where={"end": {"$lt": before}})

    def count(self) -> int:
        return self.col.count()
