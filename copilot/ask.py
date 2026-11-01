"""Ask GenBot about the system.

    python -m copilot.ask "Why are checkouts failing?"
    python -m copilot.ask "Is anything wrong right now?" -v
    python -m copilot.ask "..." --provider gemini
"""
import argparse
import sys

from . import config
from .agent import Agent, Step
from .llm import PROVIDERS, ChatClient, LLMError
from .logstore import LogStore
from .tools import Toolbox
from .vectorstore import WindowIndex


def build_agent(provider: str, model: str | None, on_step=None) -> Agent:
    toolbox = Toolbox(
        config.PROMETHEUS_URL,
        WindowIndex(config.CHROMA),
        LogStore(config.LOG_DB),
        github_repo=config.GITHUB_REPO,
        github_token=config.GITHUB_TOKEN,
    )
    return Agent(ChatClient(provider, model), toolbox, on_step=on_step)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question")
    ap.add_argument("--provider", default=config.LLM_PROVIDER, choices=list(PROVIDERS))
    ap.add_argument("--model", default=config.LLM_MODEL)
    ap.add_argument("-v", "--verbose", action="store_true", help="print each tool's full output")
    args = ap.parse_args()
    # Models use typographic Unicode (non-breaking hyphens, thin spaces) that the Windows console's
    # default code page cannot encode.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    def show(step: Step) -> None:
        arg_str = ", ".join(f"{k}={v!r}" for k, v in step.args.items())
        print(f"-> {step.tool}({arg_str})  [{step.seconds:.1f}s]", flush=True)
        if args.verbose:
            print("   " + step.result.replace("\n", "\n   "), flush=True)

    try:
        agent = build_agent(args.provider, args.model, on_step=show)
        print(f"GenBot ({agent.llm.provider}: {agent.llm.model}) investigating...\n", flush=True)
        result = agent.ask(args.question)
    except LLMError as e:
        sys.exit(f"LLM error: {e}")

    print("\n" + result.answer)
    print(f"\n[{len(result.steps)} tool calls, {result.llm_calls} LLM calls, {result.tokens} tokens, "
          f"{result.seconds:.0f}s]")


if __name__ == "__main__":
    main()
