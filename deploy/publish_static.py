"""Upload the static replay page (dist/static-space) to a Hugging Face static Space.

    pip install -r deploy/requirements.txt
    python deploy/build_static.py
    python deploy/publish_static.py --repo <username>/<space-name>

Reads HF_TOKEN (a write token) from .env. The page contains only recorded results: no keys, no code.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from copilot import config  # noqa: E402,F401  (loads .env)

ALLOWED = {"index.html", "README.md"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="<username>/<space-name>")
    args = ap.parse_args()

    from huggingface_hub import HfApi

    token = os.getenv("HF_TOKEN")
    if not token:
        sys.exit("HF_TOKEN is not set in .env (create a write token at https://huggingface.co/settings/tokens)")
    folder = ROOT / "dist" / "static-space"
    files = {p.name for p in folder.iterdir()} if folder.exists() else set()
    if "index.html" not in files:
        sys.exit("dist/static-space/index.html is missing; run python deploy/build_static.py first")
    if extra := files - ALLOWED:
        sys.exit(f"refusing to upload unexpected files: {sorted(extra)}")

    api = HfApi(token=token)
    info = api.space_info(args.repo)
    if info.sdk != "static":
        sys.exit(f"{args.repo} is a {info.sdk} Space, not a static one")
    api.upload_folder(folder_path=str(folder), repo_id=args.repo, repo_type="space",
                      commit_message="Update GenBot replay page")
    print(f"uploaded to https://huggingface.co/spaces/{args.repo}")


if __name__ == "__main__":
    main()
