"""Review the incidents GenBot has saved, and confirm or reject its diagnoses.

    python -m copilot.incidents list
    python -m copilot.incidents show INC-003
    python -m copilot.incidents resolve INC-003 --fix "Failed over to the backup payment provider"
    python -m copilot.incidents resolve INC-003 --fix "..." --cause "Expired TLS cert on the gateway"
    python -m copilot.incidents reject INC-004 --reason "Was a load test, not an outage"

Only resolved incidents are offered to the agent as proven fixes; rejected ones are never offered.
"""
import argparse
import sys

from . import config
from .memory import Incident, IncidentMemory


def show(inc: Incident) -> None:
    print(f"{inc.id}  [{inc.status.upper()}]  {inc.date}  confidence: {inc.confidence or '-'}")
    print(f"  Title:      {inc.title}")
    print(f"  Symptoms:   {inc.symptoms}")
    print(f"  Root cause: {inc.root_cause}")
    print(f"  Suggested:  {inc.suggested_fix or '-'}")
    if inc.resolution:
        label = "Fix:" if inc.status == "resolved" else "Rejected:"
        print(f"  {label:<12}{inc.resolution}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("-n", type=int, default=20)
    sub.add_parser("show").add_argument("id")
    r = sub.add_parser("resolve")
    r.add_argument("id")
    r.add_argument("--fix", required=True, help="what actually fixed it")
    r.add_argument("--cause", help="corrected root cause, if GenBot's was wrong in detail")
    j = sub.add_parser("reject")
    j.add_argument("id")
    j.add_argument("--reason", required=True)
    args = ap.parse_args()

    mem = IncidentMemory(config.INCIDENT_DB, config.CHROMA)
    if args.cmd == "list":
        incidents = mem.recent(args.n)
        if not incidents:
            print("No incidents yet.")
        for inc in incidents:
            print(f"{inc.id}  {inc.status:<8}  {inc.date}  {inc.title}")
        return

    try:
        if args.cmd == "show":
            inc = mem.get(args.id)
            if inc is None:
                raise KeyError(f"no incident {args.id}")
        elif args.cmd == "resolve":
            inc = mem.resolve(args.id, args.fix, args.cause)
        else:
            inc = mem.reject(args.id, args.reason)
    except (KeyError, ValueError) as e:
        sys.exit(f"error: {e}")
    show(inc)


if __name__ == "__main__":
    main()
