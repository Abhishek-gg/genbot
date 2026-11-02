"""Assemble the Hugging Face Space folder (dist/hf-space) from this repo.

    python deploy/build_space.py
    docker build -t genbot-space dist/hf-space                          # test it locally
    docker run --rm -p 7860:7860 -e GROQ_API_KEY=... genbot-space       # then open http://localhost:7860

Only what the Space needs is copied, from an explicit list: never .env, data/, logs/ or docs/.
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "hf-space"

# (source in the repo, destination in the Space)
INCLUDE = [
    ("copilot", "copilot"),
    ("shopapi", "shopapi"),
    ("loadgen/loadgen.py", "loadgen/loadgen.py"),
    ("scripts", "scripts"),
    ("infra/prometheus/alerts.yml", "infra/prometheus/alerts.yml"),
    ("eval/results", "eval/results"),
    ("requirements.txt", "requirements.txt"),
    ("deploy/hf/Dockerfile", "Dockerfile"),
    ("deploy/hf/README.md", "README.md"),
    ("deploy/hf/supervisord.conf", "deploy/supervisord.conf"),
    ("deploy/hf/prometheus.yml", "deploy/prometheus.yml"),
    ("deploy/hf/alertmanager.yml", "deploy/alertmanager.yml"),
]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "Dockerfile.local")
FORBIDDEN = {".env", "data", "logs", "docs", ".git", ".venv"}


def build() -> Path:
    if OUT.exists():
        shutil.rmtree(OUT)
    for src, dest in INCLUDE:
        s, d = ROOT / src, OUT / dest
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, ignore=IGNORE)
        else:
            shutil.copy2(s, d)
    leaked = [p for p in OUT.rglob("*") if p.name in FORBIDDEN]
    if leaked:
        raise SystemExit(f"refusing to build: forbidden paths in the Space: {leaked}")
    return OUT


if __name__ == "__main__":
    out = build()
    files = [p for p in out.rglob("*") if p.is_file()]
    print(f"built {out} ({len(files)} files, {sum(p.stat().st_size for p in files) / 1e6:.1f} MB)")
