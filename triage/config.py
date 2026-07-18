"""Central configuration — everything comes from the environment / .env.

Loading order:
  1. real process environment (wins — this is how docker-compose & CI inject secrets)
  2. <project>/.env   (local dev convenience; gitignored)
"""

from __future__ import annotations

import os
import pathlib

from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

# load_dotenv does NOT override already-set env vars, so the real environment wins.
load_dotenv(ENV_FILE)


def _b(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ── Splunk ──────────────────────────────────────────────────────────────────
SPLUNK_HOST = _b("SPLUNK_HOST", "127.0.0.1")
SPLUNK_MGMT_PORT = int(_b("SPLUNK_MGMT_PORT", "8089"))
SPLUNK_HEC_PORT = int(_b("SPLUNK_HEC_PORT", "8088"))
SPLUNK_API_TOKEN = _b("SPLUNK_API_TOKEN")
SPLUNK_HEC_TOKEN = _b("SPLUNK_HEC_TOKEN")
SPLUNK_USER = _b("SPLUNK_USER", "admin")
SPLUNK_PASSWORD = _b("SPLUNK_PASSWORD", "changeme-dev-1")
SPLUNK_WEB_BASE = _b("SPLUNK_WEB_BASE", "http://localhost:8000").rstrip("/")
SPLUNK_WEB_LOCALE = _b("SPLUNK_WEB_LOCALE", "en-US")

TRIAGE_INDEX = _b("TRIAGE_INDEX", "triage_demo")
TRIAGE_SOURCETYPE = _b("TRIAGE_SOURCETYPE", "alert_triage:app")

SPLUNK_MGMT_BASE = f"https://{SPLUNK_HOST}:{SPLUNK_MGMT_PORT}"
SPLUNK_HEC_URL = f"https://{SPLUNK_HOST}:{SPLUNK_HEC_PORT}/services/collector/event"

# TLS verification for Splunk. Default OFF because the local splunk-dev container
# ships a self-signed cert. In PRODUCTION set SPLUNK_VERIFY_SSL=true (or a CA bundle
# path) so connections are authenticated and MITM-resistant.
_verify_raw = _b("SPLUNK_VERIFY_SSL", "false")
if _verify_raw.lower() in ("false", "0", "no", ""):
    SPLUNK_VERIFY_SSL: "bool | str" = False
elif _verify_raw.lower() in ("true", "1", "yes"):
    SPLUNK_VERIFY_SSL = True
else:
    SPLUNK_VERIFY_SSL = _verify_raw  # treat as path to a CA bundle

# ── SMTP / mailpit ──────────────────────────────────────────────────────────
SMTP_HOST = _b("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(_b("SMTP_PORT", "1025"))
SMTP_FROM = _b("SMTP_FROM", "alert-triage@local.test")
SMTP_TO = _b("SMTP_TO", "soc-oncall@local.test")
MAILPIT_API = _b("MAILPIT_API", "http://localhost:8025").rstrip("/")

# ── Anthropic ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = _b("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _b("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = int(_b("CLAUDE_MAX_TOKENS", "2048"))

# ── Service ports ───────────────────────────────────────────────────────────
WEBHOOK_PORT = int(_b("WEBHOOK_PORT", "5001"))
MCP_PORT = int(_b("MCP_PORT", "8050"))
MCP_TRANSPORT = _b("MCP_TRANSPORT", "sse")

# URL the orchestrator (the AI agent) dials to reach the MCP connector — i.e. the
# MCP server's SSE endpoint. In compose the agent reaches it by service name; on the
# host it's localhost. This is what puts the MCP connector IN the request path.
MCP_SERVER_URL = _b("MCP_SERVER_URL", f"http://mcp-server:{MCP_PORT}/sse")


def splunk_auth_ready() -> bool:
    return bool(SPLUNK_API_TOKEN)


def summary() -> dict:
    """Non-secret view of config for /health and logs."""
    return {
        "splunk_mgmt": SPLUNK_MGMT_BASE,
        "splunk_hec": SPLUNK_HEC_URL,
        "splunk_api_token_set": bool(SPLUNK_API_TOKEN),
        "splunk_hec_token_set": bool(SPLUNK_HEC_TOKEN),
        "index": TRIAGE_INDEX,
        "sourcetype": TRIAGE_SOURCETYPE,
        "smtp": f"{SMTP_HOST}:{SMTP_PORT}",
        "anthropic_key_set": bool(ANTHROPIC_API_KEY),
        "model": CLAUDE_MODEL,
    }
