"""Paths and tunables, overridable by environment variables or a .env file in the repo root."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env support: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(ROOT / ".env")

LOG_FILE = Path(os.getenv("COPILOT_LOG_FILE", ROOT / "logs" / "shopapi.log"))
DATA_DIR = Path(os.getenv("COPILOT_DATA_DIR", ROOT / "data"))
LOG_DB = DATA_DIR / "logs.db"
INCIDENT_DB = DATA_DIR / "incidents.db"
# Chroma server from docker-compose. A folder path here switches to embedded mode (single process only).
CHROMA = os.getenv("CHROMA_URL", "http://localhost:8001")

WINDOW_S = int(os.getenv("COPILOT_WINDOW_S", "30"))  # log lines are summarized per window
GRACE_S = 5  # wait this long past a window's end before indexing it
RETENTION_DAYS = float(os.getenv("COPILOT_RETENTION_DAYS", "7"))

# Agent
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
GITHUB_REPO = os.getenv("GITHUB_REPO", "Abhishek-gg/genbot")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # optional; raises the GitHub API rate limit
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")
LLM_MODEL = os.getenv("LLM_MODEL")  # defaults per provider, see llm.PROVIDERS
