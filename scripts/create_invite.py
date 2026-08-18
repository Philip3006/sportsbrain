#!/usr/bin/env python3
"""Admin-only: mint a one-time SportsBrain invite token via POST /invite.

P0C-002/C1 replacement for the removed browser _createInvite() master-token flow.
The Cloudflare Worker's /invite endpoint requires the master token; this script
reads the master token from a SERVER-SIDE environment variable and never writes
it into any browser file, URL, or logged line.

USAGE (interactive, from a workstation with the master token in env):
    export SPORTSBRAIN_MASTER_TOKEN="<secret>"        # or SIGNALS_API_TOKEN
    python3 scripts/create_invite.py --note "For Alice"

OUTPUT (only): the resulting one-time invite link (or bare token with --raw).
The master token is NEVER echoed, logged, or included in the output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_WORKER_ORIGIN = "https://sportsbrain-signals.sportsbrain-philip.workers.dev"
DEFAULT_PWA_URL = "https://philip3006.github.io/sportsbrain/"
MASTER_TOKEN_ENV_VARS = ("SPORTSBRAIN_MASTER_TOKEN", "SIGNALS_API_TOKEN", "API_TOKEN")


def _resolve_master_token() -> str:
    for name in MASTER_TOKEN_ENV_VARS:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    print(
        "ERROR: master token not set. Export one of: "
        + ", ".join(MASTER_TOKEN_ENV_VARS),
        file=sys.stderr,
    )
    sys.exit(2)


def _mint_invite(worker_origin: str, master_token: str, note: str) -> str:
    url = worker_origin.rstrip("/") + "/invite"
    body = json.dumps({"note": note} if note else {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + master_token,
            "Content-Type": "application/json",
            "User-Agent": "sportsbrain-create-invite",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # NEVER include the master token in any error output.
        print(f"ERROR: /invite returned HTTP {exc.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"ERROR: network failure contacting Worker: {exc.reason}", file=sys.stderr)
        sys.exit(1)
    invite = payload.get("invite_token")
    if not invite:
        print(f"ERROR: Worker response missing invite_token: {payload}", file=sys.stderr)
        sys.exit(1)
    return invite


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--note", default="", help="Optional free-text note (max 200 chars)")
    parser.add_argument(
        "--worker-origin",
        default=os.environ.get("SPORTSBRAIN_WORKER_ORIGIN", DEFAULT_WORKER_ORIGIN),
        help="Cloudflare Worker origin (default: production Worker)",
    )
    parser.add_argument(
        "--pwa-url",
        default=os.environ.get("SPORTSBRAIN_PWA_URL", DEFAULT_PWA_URL),
        help="PWA base URL used to build the invite link (default: production PWA)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print only the raw invite_token (no invite URL)",
    )
    args = parser.parse_args()

    master_token = _resolve_master_token()
    invite = _mint_invite(args.worker_origin, master_token, args.note)
    if args.raw:
        print(invite)
    else:
        sep = "&" if "?" in args.pwa_url else "?"
        print(f"{args.pwa_url}{sep}invite={invite}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
