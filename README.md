# Vigil — Agentic Automated Incident Triage for Splunk

Vigil is an **agentic** incident-triage pipeline for Splunk: a saved-search alert fires a
**webhook** → an **orchestrator** starts a Claude tool-use conversation → Claude
autonomously drives a chain of **SPL investigation queries** through three MCP tools
(search → populate-if-empty → re-search → email) → an AI-written triage report — severity,
root-cause hypothesis, breakdowns, and a clickable Splunk deep link — lands in your inbox.

This isn't a static dashboard or a fixed alert template. The agent decides, on each run,
what to search, whether the result set needs enriching, and how to characterise the
incident — the same three tools are exposed both to the orchestrator's tool-use loop and
as a standalone MCP server, so any MCP-compatible client can drive the same investigation.

```
 Splunk saved-search alert  (index=triage_demo, error_count > 0, every 1 min)
        │  webhook action (HTTP POST JSON)
        ▼
 ┌───────────────────────────┐        ┌──────────────────────────────┐
 │ webhook listener (FastAPI)│  call  │ orchestrator (Anthropic loop) │
 │   POST /webhook  :5001    │ ─────► │   model = claude-sonnet-4-6   │
 └───────────────────────────┘        └───────────────┬──────────────┘
                                                       │ tool-use
            ┌──────────────────────────────────────────┼─────────────────────────┐
            ▼                                           ▼                          ▼
   search_splunk_logs                      populate_splunk_test_data        send_email
   (Splunk REST :8089)                     (Splunk HEC :8088)               (SMTP/mailpit :1025)
            └────────────── same triage.tools module also exposed by the MCP server ┘
                                  (triage.mcp_server, MCP/SSE :8050)
```

The **three tools live in one module** (`triage/tools.py`). That module is exposed two
ways: as MCP tools by `triage/mcp_server.py` (the "one MCP server"), and called directly
by the orchestrator's tool-use loop. One implementation, two surfaces.

---

## Components

| Path | Role |
|---|---|
| `triage/tools.py` | The 3 tools: `search_splunk_logs`, `populate_splunk_test_data`, `send_email` (shared) |
| `triage/splunk_client.py` | Splunk REST search + HEC inject (Bearer-token auth only) |
| `triage/deeplink.py` | Builds the clickable Splunk Web search URL for the email |
| `triage/mcp_server.py` | FastMCP server exposing the 3 tools (MCP/SSE on :8050) |
| `triage/orchestrator.py` | Anthropic tool-use loop — the agent brain |
| `triage/webhook.py` | FastAPI listener: `/webhook`, `/test-triage`, `/health` |
| `scripts/setup_splunk.py` | Mints tokens, creates index + HEC + the webhook alert |
| `scripts/trigger_alert.py` | Injects events to fire the alert, or `--manual` posts a synthetic alert |
| `scripts/verify_email.py` | Polls mailpit and prints the delivered email |
| `tests/test_e2e.py` | Drives the whole chain and asserts the email arrived |
| `docker-compose.yml` | Brings up the MCP server + webhook/orchestrator |

---

## Prerequisites (this dev box)

These already run as standalone dev containers (Docker Desktop auto-starts them):

| Container | Ports | Used for |
|---|---|---|
| `splunk-dev` (Splunk Enterprise) | 8000 web, 8089 REST, 8088 HEC | searches + data injection |
| `mailpit` (test SMTP) | 1025 SMTP, 8025 web UI | receiving the triage email |

Check: `docker ps` should show both. Python 3.12 on the host is only needed for the
`scripts/` helpers (`py` on this machine — the bare `python` alias is the broken MS Store stub).

---

## Quick start

```powershell
cd vigil-agentic-triage

# 1. Prepare Splunk: mint tokens, create index + HEC + the webhook alert.
#    Writes SPLUNK_API_TOKEN + SPLUNK_HEC_TOKEN into .env (created from .env.example).
py scripts\setup_splunk.py

# 2. (OPTIONAL) Add your Claude API key to .env  (>>> SUBSTITUTE <<<)
#    ANTHROPIC_API_KEY=sk-ant-...
#    Leave it blank to run the deterministic "scripted" mode (see Run modes below) —
#    that is how the boss demo is driven, no key required.

# 3. Bring up the pipeline (MCP server + webhook/orchestrator).
docker compose up --build -d

# 4a. Immediate end-to-end run (no waiting for Splunk's scheduler):
py scripts\trigger_alert.py --manual

# 4b. ...or the real path: inject errors and let the scheduled alert fire (~1 min):
py scripts\trigger_alert.py --count 30

# 5. Verify the email arrived.
py scripts\verify_email.py --subject "[Triage]"
#    ...or just open the mailbox: http://localhost:8025
```

Automated check of the whole chain:

```powershell
py tests\test_e2e.py
```

---

## How the agent behaves

On each alert the orchestrator (Claude) is instructed to:

1. `search_splunk_logs` for the alert's index over the **last 15 minutes**.
2. If that returns **no/insufficient** data → `populate_splunk_test_data` (realistic
   sample events via HEC, stamped *now*) → `search_splunk_logs` **again** to confirm.
3. Summarise: counts, top error codes / affected services, a **P1–P4 severity**, next actions.
4. `send_email` once — subject starts `[Triage]`, body includes the alert name, findings,
   the **exact SPL**, the severity, and the **Splunk deep link** (in `links`).

The `/test-triage` endpoint seeds an *empty* result set on purpose, so it always
exercises the populate-then-re-search branch.

### Run modes (with or without an API key)

`run_triage()` reports its `mode` explicitly:

- **`agentic`** — `ANTHROPIC_API_KEY` is set. Claude drives the tools in a real
  tool-use loop and chooses the sequence itself.
- **`scripted`** — no key. A deterministic stand-in performs the *identical*
  documented procedure (search → populate-if-empty → re-search → `stats` breakdowns →
  email) with no model call, so the full pipeline — and the rich HTML report — is
  demonstrable offline. **This is the mode the boss demo runs in.** Flip to `agentic`
  any time by adding the key and `docker compose up -d --force-recreate`.

Either way the email is built by `triage/report.py` from real Splunk `stats` results,
so the breakdowns (top error codes, affected services, regions, latency, severity) are
genuine aggregates of the indexed events — not hard-coded.

### Resetting the demo data

Injected events stay inside the 15-minute search window for ~15 min, so repeated fires
within that window stack up. For a pristine single-incident screenshot, clear the index
first (admin Bearer token, config-safe — no index/HEC teardown):

```powershell
# deletes all events in triage_demo; the next fire re-populates a clean, skewed batch
curl.exe -sk -H "Authorization: Bearer $env:SPLUNK_API_TOKEN" `
  https://127.0.0.1:8089/services/search/jobs `
  --data-urlencode "search=search index=triage_demo | delete" `
  -d exec_mode=oneshot -d output_mode=json -d earliest_time=-24h -d latest_time=now
```

---

## Verifying the email step

- **Web UI:** open <http://localhost:8025> — the triage email appears at the top.
- **CLI:** `py scripts\verify_email.py --subject "[Triage]"` prints From/To/Subject/body
  and exits 0 on success.
- **API:** `curl http://localhost:8025/api/v1/messages` returns the JSON message list.

---

## >>> SUBSTITUTE for your environment <<<

Everything is env-driven via `.env` (copied from `.env.example`). Flagged values:

| Variable | Default (this dev box) | Substitute when… |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(empty)* | **always** — your Claude key `sk-ant-...` |
| `SPLUNK_PASSWORD` | `changeme-dev-1` | your `splunk-dev` admin password differs |
| `SPLUNK_API_TOKEN` / `SPLUNK_HEC_TOKEN` | *(minted)* | auto-filled by `setup_splunk.py`; replace if pointing at a different Splunk |
| `SPLUNK_HOST` | `host.docker.internal` | running processes on the host → `127.0.0.1`; real Splunk Cloud → its hostname |
| `SPLUNK_WEB_BASE` / `SPLUNK_WEB_LOCALE` | `http://localhost:8000` / `en-US` | Splunk Cloud → stack URL + `en-GB` |
| `SPLUNK_VERIFY_SSL` | `false` | **production** → `true` (or a CA-bundle path) |
| `SMTP_HOST` / `SMTP_PORT` | `host.docker.internal` / `1025` | a real mail relay |
| `SMTP_TO` / `SMTP_FROM` | `*.local.test` | real recipient/sender |

> **Splunk Cloud note:** the previous Victoria trial (`prd-p-6oxft`) was decommissioned,
> so this pipeline targets the local `splunk-dev` container. To repoint at Splunk Cloud,
> set `SPLUNK_HOST` to the stack host, supply an ACS/HEC token, and create the alert via
> the ACS API instead of `setup_splunk.py`'s REST call. All runtime auth stays Bearer-token.

---

## Auth model

Runtime auth is **Bearer tokens only** — no `admin:password`, no Basic header, no `-u`
(matches the repo-wide rule). `setup_splunk.py` performs a single bootstrap form-login
(`/services/auth/login`, a session key — not a Basic header) purely to *mint* the JWT
auth token and HEC token; every subsequent call uses those tokens.

## The MCP server on its own

`triage/mcp_server.py` is a standalone MCP server you can point any MCP client at:

```powershell
# SSE on :8050 (default)
py -m triage.mcp_server
# or stdio
$env:MCP_TRANSPORT="stdio"; py -m triage.mcp_server
```

It exposes exactly `search_splunk_logs`, `populate_splunk_test_data`, `send_email`.

---

## Verification

See [`VERIFICATION.md`](VERIFICATION.md) for a full end-to-end verification record — the
exact tool-call sequence observed, the rendered report contents, and the reproduction
steps used to confirm the pipeline works as described.

## License

MIT — see [`LICENSE`](LICENSE).
