"""Resolve an awakeable approval from the command line.

Usage:
    python approval.py <awakeable_id> approve
    python approval.py <awakeable_id> reject [reason]     # graceful decline
    python approval.py <awakeable_id> reject [reason] --hard   # terminal failure

The awakeable id is printed by the agent when it requests approval
(and visible in the Restate UI journal). Requires the Restate ingress
on localhost:8080 (override with --ingress).

A graceful reject RESOLVES the awakeable with 'reject: <reason>'; the
agent wakes up, sees the rejection and tells the customer politely.
A hard reject REJECTS the awakeable via Restate's /reject endpoint,
which raises a terminal error inside the waiting handler - use it for
hard failures (invalid approval request, lost case, ...), not business
decisions.
"""

import argparse
import json
import sys
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("awakeable_id", help="e.g. sign_1PePOqp (from agent logs)")
    ap.add_argument("decision", choices=["approve", "reject"])
    ap.add_argument("reason", nargs="?", default="")
    ap.add_argument("--ingress", default="http://localhost:8080")
    ap.add_argument("--hard", action="store_true",
                    help="reject as a terminal failure (raises in the agent)")
    args = ap.parse_args()

    if args.decision == "approve":
        url = f"{args.ingress}/restate/awakeables/{args.awakeable_id}/resolve"
        body = json.dumps("approve").encode()
        headers = {"Content-Type": "application/json"}
    elif args.hard:
        url = f"{args.ingress}/restate/awakeables/{args.awakeable_id}/reject"
        body = (args.reason or "rejected by manager").encode()
        headers = {"Content-Type": "text/plain"}
    else:
        url = f"{args.ingress}/restate/awakeables/{args.awakeable_id}/resolve"
        body = json.dumps(f"reject: {args.reason or 'rejected by manager'}").encode()
        headers = {"Content-Type": "application/json"}

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"{args.decision} sent ({resp.status}) - the agent resumes now.")


if __name__ == "__main__":
    main()
