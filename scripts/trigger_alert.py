#!/usr/bin/env python3
"""Fire the pipeline.

Two modes:
  (default) inject error events into the index via HEC. The scheduled saved search
            ("Alert Triage Demo") notices error_count > 0 within ~1 minute and POSTs
            its webhook -> the orchestrator triages for real.

  --manual  skip Splunk's scheduler and POST a synthetic alert straight to the
            webhook listener for an immediate end-to-end run.

Examples:
    py scripts/trigger_alert.py --host 127.0.0.1 --count 30
    py scripts/trigger_alert.py --manual --webhook http://localhost:5001
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import requests
import urllib3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from triage import config  # noqa: E402
from triage.tools import _generate_events  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def inject(host: str, port: int, index: str, sourcetype: str, count: int, scenario: str) -> None:
    if not config.SPLUNK_HEC_TOKEN:
        raise SystemExit("SPLUNK_HEC_TOKEN not set — run scripts/setup_splunk.py first.")
    url = f"https://{host}:{port}/services/collector/event"
    now = time.time()
    lines = []
    for ev in _generate_events(count, scenario):
        lines.append(json.dumps({
            "time": round(now, 3), "host": "alert-triage", "source": "trigger",
            "sourcetype": sourcetype, "index": index, "event": ev,
        }))
    r = requests.post(
        url, data="".join(lines),
        headers={"Authorization": f"Splunk {config.SPLUNK_HEC_TOKEN}"},
        verify=config.SPLUNK_VERIFY_SSL, timeout=30,
    )
    if r.status_code not in (200, 201):
        raise SystemExit(f"HEC inject failed [{r.status_code}]: {r.text[:300]}")
    print(f"[ok] injected {count} '{scenario}' events into index={index} (HEC ack: {r.json()})")
    print("     The 'Alert Triage Demo' saved search runs every minute; watch the")
    print("     webhook service logs for the incoming alert, then check mailpit (:8025).")


def manual(webhook_base: str, index: str, sourcetype: str) -> None:
    payload = {
        "search_name": "Alert Triage Demo (manual trigger)",
        "index": index,
        "sourcetype": sourcetype,
        "search": f"index={index} sourcetype={sourcetype} level=ERROR | stats count",
        "results": [],
        "results_link": f"{config.SPLUNK_WEB_BASE}/{config.SPLUNK_WEB_LOCALE}/app/search/search",
    }
    url = webhook_base.rstrip("/") + "/webhook"
    print(f"[..] POST synthetic alert -> {url}")
    r = requests.post(url, json=payload, timeout=180)
    print(f"[{'ok' if r.ok else '!!'}] webhook responded [{r.status_code}]")
    try:
        print(json.dumps(r.json(), indent=2)[:4000])
    except Exception:
        print(r.text[:2000])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="Splunk host from this machine (default 127.0.0.1)")
    ap.add_argument("--hec-port", type=int, default=config.SPLUNK_HEC_PORT)
    ap.add_argument("--index", default=config.TRIAGE_INDEX)
    ap.add_argument("--sourcetype", default=config.TRIAGE_SOURCETYPE)
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--scenario", default="errors",
                    choices=["errors", "payment_failures", "auth_failures", "mixed"])
    ap.add_argument("--manual", action="store_true", help="POST straight to the webhook listener")
    ap.add_argument("--webhook", default=f"http://localhost:{config.WEBHOOK_PORT}",
                    help="webhook listener base URL (for --manual)")
    args = ap.parse_args()

    if args.manual:
        manual(args.webhook, args.index, args.sourcetype)
    else:
        inject(args.host, args.hec_port, args.index, args.sourcetype, args.count, args.scenario)


if __name__ == "__main__":
    main()
