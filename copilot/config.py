"""Paths and tunables, overridable by environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOG_FILE = Path(os.getenv("COPILOT_LOG_FILE", ROOT / "logs" / "shopapi.log"))
DATA_DIR = Path(os.getenv("COPILOT_DATA_DIR", ROOT / "data"))
LOG_DB = DATA_DIR / "logs.db"
CHROMA_DIR = DATA_DIR / "chroma"

WINDOW_S = int(os.getenv("COPILOT_WINDOW_S", "30"))  # log lines are summarized per window
GRACE_S = 5  # wait this long past a window's end before indexing it
RETENTION_DAYS = float(os.getenv("COPILOT_RETENTION_DAYS", "7"))
