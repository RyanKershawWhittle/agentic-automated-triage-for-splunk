#!/usr/bin/env python3
"""Verify the triage email landed in mailpit.

Polls the mailpit REST API for a recent message (optionally matching a subject
substring) and prints it. Exit code 0 if found, 1 if not within the timeout.

    py scripts/verify_email.py
    py scripts/verify_email.py --subject "[Triage]" --timeout 60
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from triage import config  # noqa: E402


def latest_messages(api: str, limit: int = 20) -> list[dict]:
    r = requests.get(f"{api}/api/v1/messages", params={"limit": limit}, timeout=15)
    r.raise_for_status()
    return r.json().get("messages", [])


def message_body(api: str, msg_id: str) -> str:
    try:
        r = requests.get(f"{api}/api/v1/message/{msg_id}", timeout=15)
        r.raise_for_status()
        return r.json().get("Text", "") or r.json().get("HTML", "")
    except Exception:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=config.MAILPIT_API)
    ap.add_argument("--subject", default="", help="require this substring in the subject")
    ap.add_argument("--timeout", type=int, default=45, help="seconds to poll")
    args = ap.parse_args()

    print(f"== polling mailpit at {args.api} (up to {args.timeout}s) ==")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            msgs = latest_messages(args.api)
        except Exception as e:
            print(f"  mailpit not reachable yet: {e}")
            time.sleep(3)
            continue
        for m in msgs:
            subj = m.get("Subject", "")
            if args.subject and args.subject.lower() not in subj.lower():
                continue
            frm = (m.get("From") or {}).get("Address", "?")
            to = ", ".join(a.get("Address", "?") for a in (m.get("To") or []))
            print("\n[FOUND] triage email in mailpit:")
            print(f"  From   : {frm}")
            print(f"  To     : {to}")
            print(f"  Subject: {subj}")
            print(f"  Snippet: {m.get('Snippet', '')[:200]}")
            body = message_body(args.api, m.get("ID", ""))
            if body:
                print("  ----- body -----")
                print("  " + body.replace("\n", "\n  ")[:1500])
            print(f"\n  View in browser: {args.api}")
            sys.exit(0)
        time.sleep(3)

    print(f"\n[MISS] no matching email within {args.timeout}s. Open {args.api} to inspect.")
    sys.exit(1)


if __name__ == "__main__":
    main()
