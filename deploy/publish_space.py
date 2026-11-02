"""Publish dist/hf-space to a Hugging Face Space (private unless --public).

    pip install -r deploy/requirements.txt
    python deploy/build_space.py
    python deploy/publish_space.py --repo <your-hf-username>/genbot

Reads HF_TOKEN (a write token) and GROQ_API_KEY from .env. The Groq key is stored as a Space
*secret*, so it is never part of the Space's files.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from copilot import config  # noqa: E402,F401  (loads .env)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="<username>/<space-name>")
    ap.add_argument("--public", action="store_true", help="make the Space public (default: private)")
    args = ap.parse_args()

    from huggingface_hub import HfApi

    token, groq_key = os.getenv("HF_TOKEN"), os.getenv("GROQ_API_KEY")
    if not token:
        sys.exit("HF_TOKEN is not set in .env (create a write token at https://huggingface.co/settings/tokens)")
    if not groq_key:
        sys.exit("GROQ_API_KEY is not set in .env")
    folder = ROOT / "dist" / "hf-space"
    if not (folder / "Dockerfile").exists():
        sys.exit("dist/hf-space is missing; run python deploy/build_space.py first")

    api = HfApi(token=token)
    url = api.create_repo(args.repo, repo_type="space", space_sdk="docker", private=not args.public, exist_ok=True)
    api.add_space_secret(args.repo, "GROQ_API_KEY", groq_key)
    api.upload_folder(folder_path=str(folder), repo_id=args.repo, repo_type="space",
                      commit_message="Deploy GenBot")
    print(f"published to {url} ({'public' if args.public else 'private'}); the Space is now building")


if __name__ == "__main__":
    main()
