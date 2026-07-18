"""The three triage tools — the SINGLE shared implementation.

Imported by BOTH:
  - triage.mcp_server  (registers each as an MCP @tool)
  - triage.orchestrator (executes them inside the Anthropic tool-use loop)

Each function is plain, synchronous, JSON-serialisable in/out, and self-describing
(the docstrings double as the tool descriptions shown to the model).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from . import config
from .deeplink import splunk_search_link
from .splunk_client import hec_send, run_search

# ──────────────────────────────────────────────────────────────────────────────
# JSON-Schema definitions of the tools, for the Anthropic Messages API tool-use.
# (FastMCP derives its own schema from the type hints; this is the orchestrator's.)
# ──────────────────────────────────────────────────────────────────────────────
ANTHROPIC_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_splunk_logs",
        "description": (
            "Run an SPL search against a Splunk index over a recent time window "
            "(default: last 15 minutes) and return the matching events plus a "
            "clickable Splunk deep link. Use this first to investigate an alert."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "string", "description": "Splunk index to search, e.g. 'triage_demo'."},
                "query": {"type": "string", "description": "Optional extra SPL filter/pipeline appended after the index, e.g. \"status=error | stats count by error_code\"."},
                "sourcetype": {"type": "string", "description": "Optional sourcetype filter."},
                "earliest": {"type": "string", "description": "Search window start (Splunk time modifier). Default '-15m'."},
                "latest": {"type": "string", "description": "Search window end. Default 'now'."},
                "max_count": {"type": "integer", "description": "Max rows to return. Default 100."},
            },
            "required": ["index"],
        },
    },
    {
        "name": "populate_splunk_test_data",
        "description": (
            "Inject realistic sample log events into a Splunk index via HEC, "
            "timestamped to NOW. Use this when a search returns no/insufficient "
            "data, so a follow-up search has something to find."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "string", "description": "Target index, e.g. 'triage_demo'."},
                "sourcetype": {"type": "string", "description": "Sourcetype to tag events with. Default 'alert_triage:app'."},
                "count": {"type": "integer", "description": "Number of events to inject. Default 25."},
                "scenario": {
                    "type": "string",
                    "enum": ["errors", "payment_failures", "auth_failures", "mixed"],
                    "description": "Shape of the synthetic data. Default 'errors'.",
                },
            },
            "required": ["index"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Send a plain-text email via the configured SMTP server (mailpit in dev). "
            "Use this last to deliver the triage summary, the SPL used, and a Splunk "
            "deep link to the on-call recipient."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "The triage summary, as plain text."},
                "links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of URLs (e.g. the Splunk deep link) appended under a 'Links:' section.",
                },
                "html": {"type": "string", "description": "Optional rich HTML body (rendered by mail clients). Use this to format the report with headings, tables of top error codes/affected services, and a button-style Splunk link."},
                "to": {"type": "string", "description": "Optional recipient override; defaults to SMTP_TO."},
            },
            "required": ["subject", "body"],
        },
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Tool 1 — search
# ──────────────────────────────────────────────────────────────────────────────
def search_splunk_logs(
    index: str,
    query: str = "",
    sourcetype: str = "",
    earliest: str = "-15m",
    latest: str = "now",
    max_count: int = 100,
) -> dict[str, Any]:
    """Search a Splunk index and return events + a deep link. See ANTHROPIC_TOOLS."""
    spl = _build_spl(index, query, sourcetype)
    events = run_search(spl, earliest=earliest, latest=latest, max_count=max_count)
    return {
        "spl": spl,
        "earliest": earliest,
        "latest": latest,
        "event_count": len(events),
        "events": events[:max_count],
        "deep_link": splunk_search_link(spl, earliest, latest),
    }


def _build_spl(index: str, query: str, sourcetype: str) -> str:
    parts = [f"index={index}"]
    if sourcetype:
        parts.append(f"sourcetype={sourcetype}")
    base = " ".join(parts)
    query = (query or "").strip()
    if not query:
        return base
    # If the extra query starts a pipeline ("| stats ..."), join directly; else AND it.
    return f"{base} {query}" if query.startswith("|") else f"{base} {query}"


# ──────────────────────────────────────────────────────────────────────────────
# Tool 2 — populate
# ──────────────────────────────────────────────────────────────────────────────
def populate_splunk_test_data(
    index: str,
    sourcetype: str = "",
    count: int = 25,
    scenario: str = "errors",
) -> dict[str, Any]:
    """Inject `count` realistic events to `index` via HEC, stamped now. See ANTHROPIC_TOOLS."""
    sourcetype = sourcetype or config.TRIAGE_SOURCETYPE
    events = _generate_events(count, scenario)
    ack = hec_send(events, index=index, sourcetype=sourcetype)
    return {
        "injected": len(events),
        "index": index,
        "sourcetype": sourcetype,
        "scenario": scenario,
        "hec_ack": ack,
        "note": "Events are stamped to now; allow a few seconds for indexing before re-searching.",
    }


def _weighted_fill(weights: list[tuple[str, float]], n: int) -> list[str]:
    """Deterministically expand weighted options into exactly `n` items.

    Uses the largest-remainder method so the marginal distribution matches the
    weights as closely as integer counts allow — no randomness, fully reproducible.
    """
    if n <= 0 or not weights:
        return []
    total = sum(w for _, w in weights) or 1.0
    raw = [(name, w / total * n) for name, w in weights]
    counts = {name: int(x) for name, x in raw}
    assigned = sum(counts.values())
    # Hand the leftover slots to the largest fractional remainders first.
    by_remainder = sorted(raw, key=lambda t: t[1] - int(t[1]), reverse=True)
    j = 0
    while assigned < n:
        counts[by_remainder[j % len(by_remainder)][0]] += 1
        assigned += 1
        j += 1
    out: list[str] = []
    for name, c in counts.items():
        out.extend([name] * c)
    return out


# Per-scenario weighted distributions. Each is deliberately SKEWED so the synthetic
# incident reads like a real one — a clear dominant service, error code and region —
# instead of a flat round-robin that no real outage ever looks like.
_SCENARIOS: dict[str, dict[str, list[tuple[str, float]]]] = {
    # Default demo path: a payments-led timeout incident concentrated in eu-west-2.
    "errors": {
        "service": [("payments", 40), ("checkout", 24), ("auth", 16), ("catalog", 12), ("shipping", 8)],
        "code": [("ERR_TIMEOUT", 44), ("ERR_CONN_RESET", 24), ("ERR_5XX", 20), ("ERR_DECLINED", 12)],
        "region": [("eu-west-2", 50), ("eu-west-1", 33), ("us-east-1", 17)],
    },
    # Payment gateway rejecting traffic — declines dominate, Stripe the worst gateway.
    "payment_failures": {
        "service": [("payments", 50), ("checkout", 30), ("auth", 8), ("catalog", 7), ("shipping", 5)],
        "code": [("ERR_DECLINED", 52), ("ERR_TIMEOUT", 28), ("ERR_5XX", 12), ("ERR_CONN_RESET", 8)],
        "region": [("eu-west-2", 55), ("eu-west-1", 30), ("us-east-1", 15)],
        "gateway": [("stripe", 60), ("adyen", 25), ("worldpay", 15)],
    },
    # Credential-stuffing burst against auth, concentrated on a couple of source ranges.
    "auth_failures": {
        "service": [("auth", 70), ("checkout", 12), ("catalog", 9), ("payments", 6), ("shipping", 3)],
        "code": [("ERR_BAD_CREDENTIALS", 64), ("ERR_RATE_LIMITED", 24), ("ERR_ACCOUNT_LOCKED", 12)],
        "region": [("eu-west-1", 52), ("us-east-1", 33), ("eu-west-2", 15)],
    },
    # Mixed: same shape as errors but with ~1/3 healthy events interleaved.
    "mixed": {
        "service": [("payments", 38), ("checkout", 25), ("auth", 17), ("catalog", 12), ("shipping", 8)],
        "code": [("ERR_TIMEOUT", 42), ("ERR_CONN_RESET", 23), ("ERR_5XX", 21), ("ERR_DECLINED", 14)],
        "region": [("eu-west-2", 48), ("eu-west-1", 34), ("us-east-1", 18)],
    },
}


def _generate_events(count: int, scenario: str) -> list[dict[str, Any]]:
    """Build a deterministic, realistically-SKEWED batch of synthetic log events.

    The three dimensions (service / error_code / region) are each drawn from a
    weighted distribution and decorrelated with co-prime strides, so the resulting
    incident has a believable shape (one service and one error code clearly leading)
    rather than every category landing on an identical count.
    """
    spec = _SCENARIOS.get(scenario, _SCENARIOS["errors"])
    services = _weighted_fill(spec["service"], count)
    codes = _weighted_fill(spec["code"], count)
    regions = _weighted_fill(spec["region"], count)
    gateways = _weighted_fill(spec.get("gateway", [("stripe", 60), ("adyen", 25), ("worldpay", 15)]), count)

    out: list[dict[str, Any]] = []
    for i in range(count):
        # Co-prime strides (7, 13) vs typical batch sizes (20/25/30) preserve each
        # marginal distribution exactly while breaking lock-step correlation.
        svc = services[i]
        code = codes[(i * 7 + 1) % count]
        region = regions[(i * 13 + 5) % count]
        gateway = gateways[(i * 7 + 3) % count]

        # Healthy events only appear in the "mixed" scenario (~1 in 3).
        is_error = scenario != "mixed" or (i % 3 != 0)
        # Latency tracks the failure mode: timeouts/resets are slow; others normal.
        if is_error and code in ("ERR_TIMEOUT", "ERR_CONN_RESET"):
            latency = 820 + (i % 9) * 140        # ~820–1940 ms
        elif is_error:
            latency = 180 + (i % 6) * 70          # ~180–530 ms
        else:
            latency = 40 + (i % 6) * 25           # healthy: ~40–165 ms

        base = {
            "service": svc,
            "region": region,
            "host": f"{svc}-{i % 4:02d}",
            "latency_ms": latency,
            "request_id": f"req-{10000 + i}",
        }
        if scenario == "payment_failures":
            base.update({
                "level": "ERROR" if is_error else "INFO",
                "status": "failed" if is_error else "ok",
                "gateway": gateway,
                "error_code": code if is_error else "",
                "amount": 19.99 + (i % 20) * 5,
                "currency": "GBP",
                "message": f"payment declined by {gateway} gateway" if is_error else "payment captured",
            })
        elif scenario == "auth_failures":
            base.update({
                "level": "ERROR" if is_error else "INFO",
                "status": "failed" if is_error else "ok",
                "user": f"user{i % 12}",
                "src_ip": f"10.0.{i % 3}.{(i * 37) % 254}",
                "error_code": code if is_error else "",
                "message": "authentication failure" if is_error else "login ok",
            })
        else:  # "errors" or "mixed"
            base.update({
                "level": "ERROR" if is_error else "INFO",
                "status": "error" if is_error else "ok",
                "http_status": 500 if is_error else 200,
                "error_code": code if is_error else "",
                "message": "unhandled exception in request pipeline" if is_error else "request ok",
            })
        out.append(base)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Tool 3 — email
# ──────────────────────────────────────────────────────────────────────────────
def send_email(
    subject: str,
    body: str,
    links: list[str] | None = None,
    to: str | None = None,
    html: str | None = None,
) -> dict[str, Any]:
    """Send an email via SMTP (mailpit in dev). Plain-text `body` is always sent; if
    `html` is supplied it is added as a rich alternative that mail clients render.
    See ANTHROPIC_TOOLS."""
    recipient = to or config.SMTP_TO
    full_body = body
    if links:
        full_body = body.rstrip() + "\n\nLinks:\n" + "\n".join(f"  - {u}" for u in links)

    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(full_body)
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
        smtp.send_message(msg)

    return {"sent": True, "to": recipient, "subject": subject, "bytes": len(full_body), "html": bool(html)}


# Convenience map for direct dispatch from the orchestrator.
DISPATCH = {
    "search_splunk_logs": search_splunk_logs,
    "populate_splunk_test_data": populate_splunk_test_data,
    "send_email": send_email,
}
