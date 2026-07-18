"""Alert-triage pipeline package.

Components (all share triage.tools):
  - triage.tools        the 3 tools: search_splunk_logs / populate_splunk_test_data / send_email
  - triage.splunk_client Splunk REST + HEC helpers (Bearer-token auth only)
  - triage.mcp_server    FastMCP server exposing the 3 tools (the "one MCP server")
  - triage.orchestrator  Anthropic tool-use loop (the agent brain)
  - triage.webhook       FastAPI listener that turns a Splunk alert into a triage run
"""

__version__ = "1.0.0"
