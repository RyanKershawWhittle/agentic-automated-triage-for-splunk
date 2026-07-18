#!/usr/bin/env python3
"""End-to-end test for the alert-triage pipeline.

Assumes the webhook service is running (docker compose up, or `python -m triage.webhook`)
and splunk-dev + mailpit are up. Drives the full chain via the synthetic /test-triage
entrypoint (which forces the empty -> populate -> re-search path) and then asserts a
triage email arrived in mailpit.

    py tests/test_e2e.py
    py tests/test_e2e.py --webhook http://localhost:5001 --mailpit http://localhost:8025
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import requests


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default="http://localhost:5001")
    ap.add_argument("--mailpit", default="http://localhost:8025")
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    print("== [1/4] webhook /health ==")
    h = requests.get(f"{args.webhook}/health", timeout=15)
    h.raise_for_status()
    cfg = h.json().get("config", {})
    print(json.dumps(cfg, indent=2))
    if not cfg.get("anthropic_key_set"):
        print("\n[!!] ANTHROPIC_API_KEY is not set in the service — the live model call "
              "will return 503. Set it and restart the webhook, then re-run.")

    print("\n== [2/4] note mailpit baseline ==")
    before = _count(args.mailpit)
    print(f"mailpit currently holds {before} messages")

    print("\n== [3/4] POST /test-triage (forces populate-then-research) ==")
    r = requests.post(f"{args.webhook}/test-triage", json={"alert_name": "E2E Test Alert"}, timeout=args.timeout)
    print(f"webhook responded [{r.status_code}]")
    report = r.json()
    print(json.dumps({k: v for k, v in report.items() if k != "tool_trace"}, indent=2)[:2000])
    if not report.get("ok"):
        print(f"\n[FAIL] orchestrator error: {report.get('error')}")
        return 2
    if not report.get("email_sent"):
        print("\n[FAIL] orchestrator finished but did not report email_sent=True")
        return 3
    tool_calls = report.get("tool_calls", [])
    assert "search_splunk_logs" in tool_calls, "expected a search call"
    assert "send_email" in tool_calls, "expected an email call"
    print(f"[ok] tool calls: {tool_calls}")

    print("\n== [4/4] confirm email in mailpit ==")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        msgs = _messages(args.mailpit)
        match = next((m for m in msgs if "triage" in (m.get("Subject", "").lower())), None)
        if match:
            print("[ok] triage email delivered:")
            print(f"     Subject: {match.get('Subject')}")
            print(f"     To     : {', '.join(a.get('Address','?') for a in (match.get('To') or []))}")
            print(f"\nPASS — full chain verified. Inspect it at {args.mailpit}")
            return 0
        time.sleep(3)
    print(f"[FAIL] no triage email in mailpit within {args.timeout}s")
    return 4


def _messages(api: str) -> list[dict]:
    try:
        return requests.get(f"{api}/api/v1/messages", params={"limit": 30}, timeout=10).json().get("messages", [])
    except Exception:
        return []


def _count(api: str) -> int:
    return len(_messages(api))


if __name__ == "__main__":
    sys.exit(main())
