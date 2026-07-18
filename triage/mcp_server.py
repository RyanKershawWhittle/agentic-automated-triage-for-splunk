"""The Alert-Triage MCP server.

Exposes the three pipeline tools over the Model Context Protocol so any MCP client
(Claude Desktop, Claude Code, the orchestrator, etc.) can drive them. The tool
bodies are the SAME functions in triage.tools that the orchestrator calls directly —
one implementation, two surfaces.

Run:
    python -m triage.mcp_server              # uses MCP_TRANSPORT (default sse on :8050)
    MCP_TRANSPORT=stdio python -m triage.mcp_server
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config, tools

mcp = FastMCP(
    "Alert Triage Splunk MCP",
    host="0.0.0.0",
    port=config.MCP_PORT,
)


@mcp.tool()
def search_splunk_logs(
    index: str,
    query: str = "",
    sourcetype: str = "",
    earliest: str = "-15m",
    latest: str = "now",
    max_count: int = 100,
) -> dict[str, Any]:
    """Run an SPL search against a Splunk index over a recent window (default last
    15 minutes) and return matching events plus a clickable Splunk deep link."""
    return tools.search_splunk_logs(index, query, sourcetype, earliest, latest, max_count)


@mcp.tool()
def populate_splunk_test_data(
    index: str,
    sourcetype: str = "",
    count: int = 25,
    scenario: str = "errors",
) -> dict[str, Any]:
    """Inject realistic sample log events into a Splunk index via HEC, timestamped to
    now. Use when a search returns no/insufficient data. Scenario one of:
    errors | payment_failures | auth_failures | mixed."""
    return tools.populate_splunk_test_data(index, sourcetype, count, scenario)


@mcp.tool()
def send_email(
    subject: str,
    body: str,
    links: list[str] | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    """Send a plain-text email via the configured SMTP server (mailpit in dev),
    optionally appending a list of links (e.g. the Splunk deep link)."""
    return tools.send_email(subject, body, links, to)


def main() -> None:
    transport = config.MCP_TRANSPORT
    print(f"[mcp] Alert-Triage MCP server starting (transport={transport}, port={config.MCP_PORT})")
    print(f"[mcp] tools: search_splunk_logs, populate_splunk_test_data, send_email")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
