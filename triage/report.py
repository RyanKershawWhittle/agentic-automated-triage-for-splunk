"""Turn raw Splunk breakdowns into an engineer-grade incident report.

Produces (subject, text_body, html_body). The HTML renders as a clean, scannable
report in any mail client (mailpit included) so it actually helps an on-call
engineer triage — not a generic "an alert fired" stub.
"""

from __future__ import annotations

from typing import Any

SEV_COLOR = {"P1": "#b00020", "P2": "#d35400", "P3": "#c79a00", "P4": "#2e7d32"}


def severity_for(count: int, service_spread: int) -> str:
    if count >= 50 or service_spread >= 5:
        return "P1"
    if count >= 20 or service_spread >= 3:
        return "P2"
    if count >= 5:
        return "P3"
    return "P4"


def root_cause_hypothesis(top_codes: list[tuple[str, int]]) -> str:
    codes = {c for c, _ in top_codes}
    net = {"ERR_TIMEOUT", "ERR_CONN_RESET"} & codes
    if net and len(net) >= 1 and any(c in ("ERR_TIMEOUT", "ERR_CONN_RESET") for c, _ in top_codes[:2]):
        return ("Timeouts / connection resets dominate — this points to an UPSTREAM "
                "DEPENDENCY or NETWORK issue (a slow/over-loaded downstream service or "
                "broker), not malformed input. Check the health and latency of the "
                "services the affected components call.")
    if "ERR_DECLINED" in codes and top_codes and top_codes[0][0] == "ERR_DECLINED":
        return ("Declines dominate — a downstream provider/gateway is actively rejecting "
                "requests. Likely a provider-side outage, credential/limit problem, or a "
                "bad config pushed recently. Check the provider status and recent changes.")
    if "ERR_5XX" in codes and top_codes and top_codes[0][0] == "ERR_5XX":
        return ("5xx server errors dominate — the fault is INSIDE the affected service "
                "(unhandled exception, bad deploy, resource exhaustion). Check the latest "
                "deploy and error logs for that service.")
    return ("Mixed error signature — no single failure mode dominates. Correlate the "
            "timeline with recent deploys/config changes in the affected services.")


def recommended_actions(top_service: str, severity: str) -> list[str]:
    acts = [
        f"Inspect the busiest failing component first: **{top_service}** — open its dashboard and recent logs.",
        "Correlate the start of the spike with any deploys, feature-flag flips, or config changes in the last hour.",
        "Check the health/latency of its immediate downstream dependencies (DB, cache, payment gateway, auth provider).",
    ]
    if severity in ("P1", "P2"):
        acts.append("Page the owning team now and open an incident channel — error volume is above the P2 threshold.")
    else:
        acts.append("Keep watching; escalate to the owning team if the rate is sustained over the next two windows.")
    acts.append("Use the Splunk link below to pivot from these aggregates to the raw events.")
    return acts


def _bar(pct: float, color: str) -> str:
    w = max(2, round(pct))
    return (f'<span style="display:inline-block;height:10px;width:{w*1.6}px;'
            f'background:{color};border-radius:2px;vertical-align:middle"></span>')


def build_report(ctx: dict[str, Any]) -> dict[str, str]:
    alert = ctx["alert_name"]
    index = ctx["index"]
    count = ctx["count"]
    window = ctx["window"]
    spl = ctx["spl"]
    deep_link = ctx["deep_link"]
    top_codes: list[tuple[str, int]] = ctx["top_codes"]
    by_service: list[tuple[str, int]] = ctx["by_service"]
    by_region: list[tuple[str, int]] = ctx["by_region"]
    latency = ctx.get("latency", {})
    populated = ctx.get("populated", False)

    severity = severity_for(count, len(by_service))
    top_service = by_service[0][0] if by_service else "n/a"
    hypothesis = root_cause_hypothesis(top_codes)
    actions = recommended_actions(top_service, severity)
    color = SEV_COLOR[severity]

    subject = f"[Triage] {alert} — {severity} INCIDENT — {count} errors/15m across {len(by_service)} service(s)"

    # ---------- plain text ----------
    def pct(n: int) -> str:
        return f"{round(100 * n / count)}%" if count else "0%"

    lines = [
        "AUTOMATED INCIDENT TRIAGE REPORT",
        "=" * 52,
        f"Alert     : {alert}",
        f"Severity  : {severity}",
        f"Index     : {index}",
        f"Window    : {window}",
        "",
        "WHAT HAPPENED",
        f"  {count} error/failure events across {len(by_service)} service(s) in the "
        f"window (baseline ~0)." + ("  [sample data was injected — index was empty]" if populated else ""),
        "",
        "TOP ERROR CODES",
    ]
    for c, n in top_codes[:6]:
        lines.append(f"  {c or '(none)':<18} {n:>4}  {pct(n)}")
    lines += ["", "AFFECTED SERVICES"]
    for s, n in by_service[:6]:
        lines.append(f"  {s or '(unknown)':<18} {n:>4}  {pct(n)}")
    lines += ["", "WHERE (regions)"]
    for rg, n in by_region[:6]:
        lines.append(f"  {rg or '(unknown)':<18} {n:>4}")
    if latency:
        lines += ["", "LATENCY", f"  avg {latency.get('avg','?')} ms / max {latency.get('max','?')} ms"]
    lines += ["", "LIKELY ROOT CAUSE", "  " + hypothesis.replace("**", "")]
    lines += ["", "RECOMMENDED ACTIONS"]
    for i, a in enumerate(actions, 1):
        lines.append(f"  {i}. {a.replace('**','')}")
    lines += ["", "INVESTIGATE IN SPLUNK", f"  {deep_link}", "",
              f"SPL USED", f"  {spl}", "",
              "— Generated automatically by Alert-Triage (search -> analyse -> email)"]
    text_body = "\n".join(lines)

    # ---------- html ----------
    code_rows = "".join(
        f'<tr><td style="padding:3px 10px 3px 0"><code>{c or "(none)"}</code></td>'
        f'<td style="padding:3px 8px;text-align:right">{n}</td>'
        f'<td style="padding:3px 8px;color:#666">{pct(n)}</td>'
        f'<td style="padding:3px 0">{_bar(100*n/count if count else 0, color)}</td></tr>'
        for c, n in top_codes[:6]
    )
    svc_rows = "".join(
        f'<tr><td style="padding:3px 10px 3px 0">{s or "(unknown)"}</td>'
        f'<td style="padding:3px 8px;text-align:right">{n}</td>'
        f'<td style="padding:3px 8px;color:#666">{pct(n)}</td></tr>'
        for s, n in by_service[:6]
    )
    region_chips = " ".join(
        f'<span style="background:#eef;border:1px solid #ccd;border-radius:10px;'
        f'padding:2px 9px;margin:2px;display:inline-block;font-size:12px">{rg or "?"}: <b>{n}</b></span>'
        for rg, n in by_region[:6]
    )
    action_items = "".join(f"<li style='margin:4px 0'>{a.replace('**','<b>',1).replace('**','</b>',1)}</li>" for a in actions)
    lat = (f"<p style='margin:6px 0;color:#444'>Latency: avg <b>{latency.get('avg','?')}</b> ms / "
           f"max <b>{latency.get('max','?')}</b> ms</p>" if latency else "")
    injected_note = ("<div style='background:#fff8e1;border:1px solid #ffe082;color:#7a5b00;"
                     "padding:6px 10px;border-radius:6px;font-size:12px;margin:8px 0'>"
                     "Demo note: the index was empty, so realistic sample events were injected "
                     "before analysis.</div>" if populated else "")

    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:680px;color:#1a1a1a">
  <div style="background:{color};color:#fff;padding:14px 18px;border-radius:8px 8px 0 0">
    <div style="font-size:12px;letter-spacing:1px;opacity:.85">AUTOMATED INCIDENT TRIAGE</div>
    <div style="font-size:20px;font-weight:700;margin-top:2px">{severity} &middot; {alert}</div>
    <div style="font-size:13px;opacity:.9;margin-top:4px">{count} error/failure events &middot; {len(by_service)} service(s) &middot; window {window}</div>
  </div>
  <div style="border:1px solid #e3e3e3;border-top:none;border-radius:0 0 8px 8px;padding:16px 18px">
    {injected_note}
    <h3 style="margin:6px 0 4px;font-size:14px;color:#333">Likely root cause</h3>
    <p style="margin:0 0 12px;line-height:1.45">{hypothesis}</p>

    <div style="display:flex;gap:24px;flex-wrap:wrap">
      <div style="min-width:280px">
        <h3 style="margin:6px 0 4px;font-size:14px;color:#333">Top error codes</h3>
        <table style="border-collapse:collapse;font-size:13px">{code_rows}</table>
      </div>
      <div style="min-width:220px">
        <h3 style="margin:6px 0 4px;font-size:14px;color:#333">Affected services</h3>
        <table style="border-collapse:collapse;font-size:13px">{svc_rows}</table>
      </div>
    </div>

    <h3 style="margin:14px 0 4px;font-size:14px;color:#333">Where</h3>
    <div>{region_chips}</div>
    {lat}

    <h3 style="margin:14px 0 4px;font-size:14px;color:#333">Recommended actions</h3>
    <ol style="margin:0;padding-left:18px;line-height:1.5;font-size:13px">{action_items}</ol>

    <div style="margin:18px 0 6px">
      <a href="{deep_link}" style="background:{color};color:#fff;text-decoration:none;
         padding:10px 16px;border-radius:6px;font-weight:600;font-size:13px;display:inline-block">
         🔍 Investigate in Splunk →</a>
    </div>
    <p style="font-size:11px;color:#888;margin:10px 0 0">SPL: <code>{spl}</code></p>
    <p style="font-size:11px;color:#aaa;margin:4px 0 0">Generated automatically by Alert-Triage — search → analyse → email.</p>
  </div>
</div>"""

    return {"subject": subject, "text": text_body, "html": html_body}
