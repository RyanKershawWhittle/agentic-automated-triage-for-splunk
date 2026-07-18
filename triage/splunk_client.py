"""Splunk REST (search) + HEC (inject) helpers.

Runtime auth is **Bearer token only** (house rule — no Basic auth, no admin:password
header at runtime). Tokens are provided via config (minted once by scripts/setup_splunk.py).
"""

from __future__ import annotations

import time
from typing import Any

import requests
import urllib3

from . import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class SplunkError(RuntimeError):
    pass


def _rest_headers() -> dict[str, str]:
    if not config.SPLUNK_API_TOKEN:
        raise SplunkError(
            "SPLUNK_API_TOKEN is not set — run scripts/setup_splunk.py to mint a "
            "Splunk auth token, then restart the service."
        )
    return {"Authorization": f"Bearer {config.SPLUNK_API_TOKEN}"}


def run_search(
    spl: str,
    earliest: str = "-15m",
    latest: str = "now",
    max_count: int = 100,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Run a blocking oneshot search and return result rows as dicts."""
    search = spl if spl.lstrip().startswith("|") else f"search {spl}"
    body = {
        "search": search,
        "exec_mode": "oneshot",
        "output_mode": "json",
        "earliest_time": earliest,
        "latest_time": latest,
        "count": max_count,
    }
    try:
        r = requests.post(
            f"{config.SPLUNK_MGMT_BASE}/services/search/jobs",
            data=body, headers=_rest_headers(), verify=config.SPLUNK_VERIFY_SSL, timeout=timeout,
        )
    except requests.RequestException as e:
        raise SplunkError(f"Splunk REST unreachable at {config.SPLUNK_MGMT_BASE}: {e}") from e
    if r.status_code != 200:
        raise SplunkError(f"Splunk search failed [{r.status_code}]: {r.text[:300]}")
    return r.json().get("results", [])


def hec_send(
    events: list[dict[str, Any]],
    index: str,
    sourcetype: str,
    source: str = "alert-triage",
    timeout: int = 30,
) -> dict[str, Any]:
    """Send events to HEC, each stamped to *now*. Returns HEC's JSON ack."""
    if not config.SPLUNK_HEC_TOKEN:
        raise SplunkError(
            "SPLUNK_HEC_TOKEN is not set — run scripts/setup_splunk.py to create a HEC token."
        )
    now = time.time()
    payload = "".join(
        _hec_line(e, index, sourcetype, source, now) for e in events
    )
    headers = {"Authorization": f"Splunk {config.SPLUNK_HEC_TOKEN}"}
    try:
        r = requests.post(
            config.SPLUNK_HEC_URL, data=payload, headers=headers, verify=config.SPLUNK_VERIFY_SSL, timeout=timeout
        )
    except requests.RequestException as e:
        raise SplunkError(f"HEC unreachable at {config.SPLUNK_HEC_URL}: {e}") from e
    if r.status_code not in (200, 201):
        raise SplunkError(f"HEC send failed [{r.status_code}]: {r.text[:300]}")
    return r.json()


def _hec_line(event: dict[str, Any], index: str, sourcetype: str, source: str, ts: float) -> str:
    import json

    envelope = {
        "time": round(ts, 3),
        "host": "alert-triage",
        "source": source,
        "sourcetype": sourcetype,
        "index": index,
        "event": event,
    }
    return json.dumps(envelope)
