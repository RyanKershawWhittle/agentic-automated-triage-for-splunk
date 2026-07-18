#!/usr/bin/env python3
"""One-time (idempotent) Splunk setup for the alert-triage pipeline.

Run from the HOST against the local splunk-dev container:

    py scripts/setup_splunk.py
    py scripts/setup_splunk.py --host 127.0.0.1 --webhook-url http://host.docker.internal:5001/webhook

What it does (Bearer-token auth, never Basic at runtime):
  1. form-login (/services/auth/login) -> session key   [bootstrap only]
  2. enable Splunk token authentication
  3. mint a Splunk JWT auth token        -> SPLUNK_API_TOKEN  (REST searches)
  4. create the target index
  5. enable HEC + create a HEC token      -> SPLUNK_HEC_TOKEN  (data injection)
  6. create the scheduled saved-search alert with a webhook action
  7. write the two tokens back into ../.env

The session key in step 1 is obtained via a form POST (not a Basic header / -u flag),
satisfying the house rule that runtime auth uses tokens only.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import requests
import urllib3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from triage import config  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_PATH = pathlib.Path(__file__).resolve().parents[1] / ".env"
ALERT_NAME = "Alert Triage Demo"


def _ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def _warn(msg: str) -> None:
    print(f"  [!!] {msg}")


def login(base: str, user: str, pw: str, verify) -> str:
    r = requests.post(
        f"{base}/services/auth/login",
        data={"username": user, "password": pw, "output_mode": "json"},
        verify=verify, timeout=30,
    )
    if r.status_code != 200:
        raise SystemExit(f"login failed [{r.status_code}]: {r.text[:300]}")
    return r.json()["sessionKey"]


def enable_token_auth(base: str, hdr: dict, verify) -> None:
    for path in (
        "/services/admin/token-auth/tokens_auth/tokens_auth",
        "/services/admin/token-auth/tokens_auth",
    ):
        try:
            r = requests.post(f"{base}{path}", data={"disabled": "0", "output_mode": "json"},
                              headers=hdr, verify=verify, timeout=30)
            if r.status_code in (200, 201):
                _ok("token authentication enabled")
                return
        except requests.RequestException:
            continue
    _warn("could not toggle token-auth endpoint (often already enabled) — continuing")


def mint_auth_token(base: str, hdr: dict, user: str, verify) -> str:
    r = requests.post(
        f"{base}/services/authorization/tokens",
        data={"name": user, "audience": "alert-triage", "output_mode": "json"},
        headers=hdr, verify=verify, timeout=30,
    )
    if r.status_code not in (200, 201):
        raise SystemExit(f"mint auth token failed [{r.status_code}]: {r.text[:400]}")
    token = r.json()["entry"][0]["content"]["token"]
    _ok(f"minted Splunk auth token (len={len(token)})")
    return token


def create_index(base: str, hdr: dict, index: str, verify) -> None:
    r = requests.post(f"{base}/services/data/indexes",
                      data={"name": index, "output_mode": "json"},
                      headers=hdr, verify=verify, timeout=30)
    if r.status_code in (200, 201):
        _ok(f"index '{index}' created")
    elif r.status_code == 409:
        _ok(f"index '{index}' already exists")
    else:
        _warn(f"create index returned [{r.status_code}]: {r.text[:200]}")


def enable_hec(base: str, hdr: dict, verify) -> None:
    for path in (
        "/servicesNS/nobody/splunk_httpinput/data/inputs/http/http",
        "/services/data/inputs/http/http",
    ):
        try:
            r = requests.post(f"{base}{path}", data={"disabled": "0", "output_mode": "json"},
                              headers=hdr, verify=verify, timeout=30)
            if r.status_code in (200, 201):
                _ok("HEC enabled globally")
                return
        except requests.RequestException:
            continue
    _warn("could not toggle global HEC (may already be enabled) — continuing")


def create_hec_token(base: str, hdr: dict, index: str, sourcetype: str, verify) -> str:
    name = "alert-triage-hec"
    data = {
        "name": name, "index": index, "indexes": index,
        "sourcetype": sourcetype, "output_mode": "json",
    }
    r = requests.post(f"{base}/services/data/inputs/http", data=data, headers=hdr, verify=verify, timeout=30)
    if r.status_code in (200, 201):
        token = r.json()["entry"][0]["content"]["token"]
        _ok(f"HEC token created (len={len(token)})")
        return token
    if r.status_code == 409:
        g = requests.get(f"{base}/services/data/inputs/http/http%3A%2F%2F{name}",
                         params={"output_mode": "json"}, headers=hdr, verify=verify, timeout=30)
        if g.status_code == 200:
            token = g.json()["entry"][0]["content"]["token"]
            _ok("HEC token already existed — reused")
            return token
    raise SystemExit(f"create HEC token failed [{r.status_code}]: {r.text[:400]}")


def create_alert(base: str, hdr: dict, index: str, sourcetype: str, webhook_url: str, verify) -> None:
    spl = (
        f"index={index} sourcetype={sourcetype} (level=ERROR OR status=error OR status=failed) "
        f"| stats count as error_count | where error_count > 0"
    )
    data = {
        "name": ALERT_NAME,
        "search": spl,
        "dispatch.earliest_time": "-5m",
        "dispatch.latest_time": "now",
        "cron_schedule": "*/1 * * * *",
        "is_scheduled": "1",
        "alert_type": "number of events",
        "alert_comparator": "greater than",
        "alert_threshold": "0",
        "alert.track": "1",
        "alert.digest_mode": "1",
        "actions": "webhook",
        "action.webhook": "1",
        "action.webhook.param.url": webhook_url,
        "output_mode": "json",
    }
    sns = f"{base}/servicesNS/nobody/search/saved/searches"
    r = requests.post(sns, data=data, headers=hdr, verify=verify, timeout=30)
    if r.status_code in (200, 201):
        _ok(f"alert '{ALERT_NAME}' created -> webhook {webhook_url}")
        return
    if r.status_code == 409:
        # Update in place.
        upd = {k: v for k, v in data.items() if k != "name"}
        r2 = requests.post(f"{sns}/{requests.utils.quote(ALERT_NAME)}", data=upd, headers=hdr, verify=verify, timeout=30)
        if r2.status_code in (200, 201):
            _ok(f"alert '{ALERT_NAME}' updated -> webhook {webhook_url}")
            return
        _warn(f"alert update returned [{r2.status_code}]: {r2.text[:200]}")
        return
    raise SystemExit(f"create alert failed [{r.status_code}]: {r.text[:400]}")


def write_env(api_token: str, hec_token: str) -> None:
    """Create .env from .env.example if missing, then set the two token lines."""
    if not ENV_PATH.exists():
        example = ENV_PATH.with_name(".env.example")
        ENV_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        _ok("created .env from .env.example")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updates = {"SPLUNK_API_TOKEN": api_token, "SPLUNK_HEC_TOKEN": hec_token}
    seen = set()
    for i, line in enumerate(lines):
        for key, val in updates.items():
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={val}"
                seen.add(key)
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _ok(f"wrote SPLUNK_API_TOKEN + SPLUNK_HEC_TOKEN to {ENV_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="Splunk host as seen from THIS machine (default 127.0.0.1)")
    ap.add_argument("--mgmt-port", type=int, default=config.SPLUNK_MGMT_PORT)
    ap.add_argument("--webhook-url", default="http://host.docker.internal:5001/webhook",
                    help="URL Splunk should POST the alert to (default: host.docker.internal:5001 for compose)")
    ap.add_argument("--index", default=config.TRIAGE_INDEX)
    ap.add_argument("--sourcetype", default=config.TRIAGE_SOURCETYPE)
    args = ap.parse_args()

    base = f"https://{args.host}:{args.mgmt_port}"
    verify = config.SPLUNK_VERIFY_SSL
    print(f"== Alert-Triage Splunk setup against {base} ==")

    print("[1/7] form-login (bootstrap)")
    session_key = login(base, config.SPLUNK_USER, config.SPLUNK_PASSWORD, verify)
    hdr = {"Authorization": f"Splunk {session_key}"}
    _ok("session key acquired")

    print("[2/7] enable token authentication")
    enable_token_auth(base, hdr, verify)

    print("[3/7] mint REST auth token")
    api_token = mint_auth_token(base, hdr, config.SPLUNK_USER, verify)

    print(f"[4/7] create index '{args.index}'")
    create_index(base, hdr, args.index, verify)

    print("[5/7] enable HEC + create token")
    enable_hec(base, hdr, verify)
    hec_token = create_hec_token(base, hdr, args.index, args.sourcetype, verify)

    print(f"[6/7] create alert '{ALERT_NAME}'")
    create_alert(base, hdr, args.index, args.sourcetype, args.webhook_url, verify)

    print("[7/7] persist tokens to .env")
    write_env(api_token, hec_token)

    print("\nDONE. Tokens written to .env. Next: bring up the pipeline (docker compose up)")
    print(f"      then trigger:  py scripts/trigger_alert.py --host {args.host}")


if __name__ == "__main__":
    main()
