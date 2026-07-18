"""The orchestrator — an Anthropic tool-use loop that triages a Splunk alert.

Given an alert context it lets Claude drive the investigation using the three
triage tools:

  1. search_splunk_logs   — look at the index over the last 15 minutes
  2. populate_splunk_test_data — if the search is empty/insufficient, inject samples
     and (the model) re-runs the search to confirm
  3. send_email           — deliver the summary + SPL + Splunk deep link

The tool bodies are triage.tools.* (the same code the MCP server exposes).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import config, report, tools

log = logging.getLogger("triage.orchestrator")

SYSTEM_PROMPT = """\
You are an autonomous SOC (Security Operations Centre) triage agent wired into Splunk.

A monitoring alert has fired. Investigate it end-to-end using ONLY the provided tools,
then notify the on-call engineer by email. Follow this procedure:

1. Call `search_splunk_logs` for the alert's index over the last 15 minutes
   (earliest="-15m"). Look at the event_count and the events.
2. If the search returns NO events or clearly insufficient data to explain the alert,
   call `populate_splunk_test_data` for that index to inject realistic sample events,
   then call `search_splunk_logs` AGAIN to confirm the data is now present. (You may
   need a slightly wider window like -16m if indexing lag hides brand-new events.)
3. Form a concise triage summary: what you searched, how many events you found, the
   notable patterns (top error codes / affected services / counts), a severity call
   (P1–P4), and one or two recommended next actions.
4. Call `send_email` exactly once to send the summary. The subject must start with
   "[Triage]". The body must include: the alert name, your findings/counts, the exact
   SPL you ran, and the severity. Pass the Splunk `deep_link` returned by your search
   in the `links` array so the engineer can jump straight to the data.

Be decisive and brief. Do not ask the user questions — you are autonomous. When the
email is sent, stop and give a one-paragraph final report of what you did.
"""

MAX_TURNS = 12


class OrchestratorError(RuntimeError):
    pass


def _client():
    if not config.ANTHROPIC_API_KEY:
        raise OrchestratorError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment or .env so the "
            "orchestrator can call Claude."
        )
    import anthropic

    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _alert_to_prompt(alert: dict[str, Any]) -> str:
    return (
        "A Splunk alert has fired. Triage it.\n\n"
        f"alert_name: {alert.get('alert_name', 'unknown')}\n"
        f"index: {alert.get('index', config.TRIAGE_INDEX)}\n"
        f"sourcetype: {alert.get('sourcetype', config.TRIAGE_SOURCETYPE)}\n"
        f"saved_search_spl: {alert.get('search', '(not provided)')}\n"
        f"time_range: {alert.get('earliest', '-15m')} .. {alert.get('latest', 'now')}\n"
        f"trigger_results: {json.dumps(alert.get('results', []))[:1500]}\n"
        f"results_link: {alert.get('results_link', '')}\n"
    )


def _run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = tools.DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    return fn(**args)


def run_triage(alert: dict[str, Any]) -> dict[str, Any]:
    """Drive the full triage for one alert. Returns a structured report.

    If ANTHROPIC_API_KEY is set, Claude drives the tools agentically. If not, we fall
    back to a deterministic SCRIPTED run that performs the identical documented
    procedure (search -> populate-if-empty -> re-search -> summarise -> email) so the
    pipeline is fully demonstrable without an API key. The mode is reported explicitly.
    """
    if not config.ANTHROPIC_API_KEY:
        return _run_triage_scripted(alert)
    return _run_triage_agentic(alert)


def _run_triage_agentic(alert: dict[str, Any]) -> dict[str, Any]:
    """The real agent: let Claude choose and sequence the tool calls."""
    client = _client()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _alert_to_prompt(alert)}
    ]
    tool_trace: list[dict[str, Any]] = []
    final_text = ""
    stop_reason = None

    for turn in range(MAX_TURNS):
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tools.ANTHROPIC_TOOLS,
            messages=messages,
        )
        stop_reason = resp.stop_reason

        # Collect any assistant text from this turn.
        for block in resp.content:
            if block.type == "text":
                final_text = block.text

        if resp.stop_reason != "tool_use":
            break

        # Append the assistant turn verbatim, then execute every tool_use block.
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            log.info("tool_use: %s(%s)", block.name, json.dumps(block.input)[:200])
            try:
                result = _run_tool(block.name, dict(block.input))
                is_error = isinstance(result, dict) and "error" in result and result.get("error")
            except Exception as e:  # surface tool failures back to the model
                result = {"error": f"{type(e).__name__}: {e}"}
                is_error = True
            tool_trace.append({"tool": block.name, "input": dict(block.input), "result": result})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str)[:8000],
                    "is_error": bool(is_error),
                }
            )
        messages.append({"role": "user", "content": tool_results})
    else:
        log.warning("hit MAX_TURNS (%d) without a natural stop", MAX_TURNS)

    email_sent = any(
        t["tool"] == "send_email" and isinstance(t["result"], dict) and t["result"].get("sent")
        for t in tool_trace
    )
    return {
        "ok": True,
        "mode": "agentic",
        "model": config.CLAUDE_MODEL,
        "stop_reason": stop_reason,
        "turns": len([t for t in tool_trace]) or 0,
        "tool_calls": [t["tool"] for t in tool_trace],
        "email_sent": email_sent,
        "summary": final_text,
        "tool_trace": tool_trace,
    }


def _run_triage_scripted(alert: dict[str, Any]) -> dict[str, Any]:
    """Deterministic triage when no API key is present — same procedure, no model.

    Mirrors the agent's documented steps so the pipeline is fully demonstrable:
    search -> populate-if-empty -> re-search -> summarise -> email.
    """
    import time

    index = alert.get("index") or config.TRIAGE_INDEX
    sourcetype = alert.get("sourcetype") or config.TRIAGE_SOURCETYPE
    alert_name = alert.get("alert_name", "Splunk Alert")
    filt = "(level=ERROR OR status=failed OR status=error)"
    trace: list[dict[str, Any]] = []

    def step(tool: str, **kw):
        log.info("[scripted] %s(%s)", tool, json.dumps(kw)[:160])
        result = _run_tool(tool, kw)
        trace.append({"tool": tool, "input": kw, "result": result})
        return result

    # 1) search the index over the last 15 minutes
    r = step("search_splunk_logs", index=index, query=filt, earliest="-15m")
    populated = False

    # 2) if empty/insufficient -> inject realistic data, then re-search to confirm
    if r["event_count"] == 0:
        scenario = "payment_failures" if "pay" in sourcetype.lower() else "errors"
        step("populate_splunk_test_data", index=index, sourcetype=sourcetype, count=30, scenario=scenario)
        populated = True
        time.sleep(4)  # allow indexing
        r = step("search_splunk_logs", index=index, query=filt, earliest="-16m")

    # 3) analyse — break the events down the way an engineer would
    def grouped(field: str) -> list[tuple[str, int]]:
        res = step("search_splunk_logs", index=index,
                   query=f"{filt} | stats count by {field} | sort -count", earliest="-16m")
        out = []
        for row in res.get("events", []):
            try:
                out.append((row.get(field), int(row.get("count", 0))))
            except (TypeError, ValueError):
                continue
        return out

    top_codes = grouped("error_code")
    by_service = grouped("service")
    by_region = grouped("region")
    lat_res = step("search_splunk_logs", index=index,
                   query=f"{filt} | stats avg(latency_ms) as avg max(latency_ms) as max", earliest="-16m")
    latency = {}
    if lat_res.get("events"):
        row = lat_res["events"][0]
        try:
            latency = {"avg": round(float(row.get("avg", 0))), "max": round(float(row.get("max", 0)))}
        except (TypeError, ValueError):
            latency = {}

    # 4) build the engineer-grade report
    report_ctx = {
        "alert_name": alert_name,
        "index": index,
        "count": r["event_count"],
        "window": "last 15 minutes",
        "spl": r["spl"],
        "deep_link": r["deep_link"],
        "top_codes": top_codes,
        "by_service": by_service,
        "by_region": by_region,
        "latency": latency,
        "populated": populated,
    }
    rep = report.build_report(report_ctx)

    # 5) email it (rich HTML + plain-text fallback, with the Splunk deep link)
    email = step("send_email", subject=rep["subject"], body=rep["text"],
                 html=rep["html"], links=[r["deep_link"]])

    severity = report.severity_for(r["event_count"], len(by_service))
    return {
        "ok": True,
        "mode": "scripted (no ANTHROPIC_API_KEY — deterministic stand-in for the model)",
        "model": None,
        "stop_reason": "scripted_complete",
        "tool_calls": [t["tool"] for t in trace],
        "email_sent": bool(isinstance(email, dict) and email.get("sent")),
        "severity": severity,
        "event_count": r["event_count"],
        "subject": rep["subject"],
        "summary": rep["text"],
        "tool_trace": trace,
    }
