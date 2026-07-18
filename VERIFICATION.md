# Agentic Automated Triage for Splunk — Verification

**Status: COMPLETE and verified end-to-end (2026-06-12).** One webhook POST produces a
rich, boss-ready HTML incident-triage email in mailpit, with real Splunk-derived
breakdowns and a working "Investigate in Splunk" deep link.

---

## 1. What it is

An autonomous Splunk **alert-triage pipeline**:

```
Splunk saved-search alert ──webhook──► FastAPI listener (:5001)
                                          │
                                          ▼
                                   orchestrator  ──► 3 tools (triage/tools.py)
                                          │            • search_splunk_logs   (REST :8089)
                                          │            • populate_splunk_test_data (HEC :8088)
                                          │            • send_email           (SMTP :1025)
                                          ▼
                          triage/report.py builds an HTML incident report
                                          ▼
                                   mailpit inbox (:8025)
```

The **three tools live in one shared module** (`triage/tools.py`) and are exposed two ways:

1. as an **MCP server** — `triage/mcp_server.py` (FastMCP, SSE on `:8050`), and
2. called **directly** by `triage/orchestrator.py`.

One implementation, two surfaces.

### Two run modes
- **`scripted`** (no `ANTHROPIC_API_KEY`) — a deterministic stand-in runs the exact same
  procedure with no model call. **This is what the demo uses.**
- **`agentic`** (key set) — Claude (`claude-sonnet-4-6`) drives the tool-use loop itself.

Both build the email from **real Splunk `stats` results**, so the breakdowns are genuine
aggregates of the indexed events, not hard-coded.

---

## 2. The verified flow (what was actually observed)

A single `POST /webhook` with an empty result set produced:

- **mode** `scripted`, **email_sent** `true`, **severity** `P1`, **event_count** `30`
- **tool_calls**: `search_splunk_logs → populate_splunk_test_data → search_splunk_logs →
  search_splunk_logs ×4 (the stats breakdowns) → send_email`
- An email in mailpit with subject:
  `[Triage] Payment Gateway Errors — P1 INCIDENT — 30 errors/15m across 5 service(s)`

The rendered HTML report (screenshot: `docs/alert-triage-email-rendered.png`) contains:

| Section | Content (this demo run) |
|---|---|
| Severity header (colour-coded) | **P1 · Payment Gateway Errors** — 30 events · 5 services · 15-min window |
| Likely root cause | Timeouts / connection resets dominate → upstream dependency / network |
| Top error codes (table + bars) | `ERR_TIMEOUT 43%`, `ERR_CONN_RESET 23%`, `ERR_5XX 20%`, `ERR_DECLINED 13%` |
| Affected services (table) | `payments 40%`, `checkout 23%`, `auth 17%`, `catalog 13%`, `shipping 7%` |
| Where (region chips) | `eu-west-2: 15`, `eu-west-1: 10`, `us-east-1: 5` |
| Latency | avg **968 ms** / max **1940 ms** |
| Recommended actions | 5 numbered, lead action targets the busiest component (`payments`) |
| Investigate in Splunk | button → `http://localhost:8000/en-US/app/search/search?q=…` (pre-loaded SPL + window) |

The narrative is **internally coherent**: the dominant error code (timeouts), the busiest
service (payments), the concentrated region (eu-west-2) and the high latency all line up
with the root-cause hypothesis and the lead recommended action.

`py tests/test_e2e.py` → **PASS** (drives `/test-triage`, asserts the chain + delivery).

---

## 3. How to run it

Prereqs (already running as standalone dev containers): `splunk-dev` and `mailpit`.

```powershell
cd projects\alert-triage

# Splunk is already set up (index triage_demo, REST+HEC tokens in .env, the
# "Alert Triage Demo" webhook alert). Re-run ONLY if tokens are missing:
#   py scripts\setup_splunk.py

# Bring the stack up (rebuild picks up any code change):
docker compose up -d --build

# (optional) pristine single-incident screenshot — clear leftover events first:
curl.exe -sk -H "Authorization: Bearer $env:SPLUNK_API_TOKEN" `
  https://127.0.0.1:8089/services/search/jobs `
  --data-urlencode "search=search index=triage_demo | delete" `
  -d exec_mode=oneshot -d output_mode=json -d earliest_time=-24h -d latest_time=now

# Fire one triage run (the demo payload):
curl.exe -s -X POST http://localhost:5001/webhook -H "Content-Type: application/json" `
  -d '{\"search_name\":\"Payment Gateway Errors\",\"index\":\"triage_demo\",\"sourcetype\":\"alert_triage:app\",\"results\":[]}'

# Watch it land:  http://localhost:8025   (newest message)
#   or CLI:        py scripts\verify_email.py --subject "[Triage]"
#   or full test:  py tests\test_e2e.py
```

Real-alert path (no manual POST): `py scripts\trigger_alert.py --count 30` injects errors;
the scheduled "Alert Triage Demo" saved search fires its webhook within ~1 minute.

> Host vs container networking: scripts run on the host use `127.0.0.1`; the containers
> use `host.docker.internal` (already set as `SPLUNK_HOST`/`SMTP_HOST` in `.env`).
> Use `py`, never the bare `python` alias (broken MS Store stub).

---

## 4. Flipping to live AI (agentic mode)

1. Put the key in `.env` on its **own line, with no inline comment**:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
2. Recreate so the container re-reads `.env`:
   ```powershell
   docker compose up -d --force-recreate
   ```
3. Confirm the key actually reached the container (not glued to a comment):
   ```powershell
   docker exec alert-triage-app printenv | Select-String ANTHROPIC
   curl.exe -s http://localhost:5001/health   # -> "anthropic_key_set": true
   ```
4. Fire the webhook again → the response `mode` becomes `agentic` and `model` is
   `claude-sonnet-4-6`. The email still arrives (Claude now sequences the tools itself).

Model is configurable via `CLAUDE_MODEL` in `.env`.

---

## 5. Known gotchas (all real, already hit)

- **`.env` inline comments leak into values.** docker-compose `env_file` does **not** strip
  `KEY=value   # comment` — the comment becomes part of the value. This previously caused a
  false `anthropic_key_set: true`. Keep every comment on its **own line**. Verify with
  `docker exec alert-triage-app printenv | grep -i anthropic`.
- **Host vs container networking.** Inside compose use `host.docker.internal`; host scripts
  use `127.0.0.1`. Scripts take `--host` (default `127.0.0.1`).
- **Self-signed TLS.** `splunk-dev` uses a self-signed cert → `SPLUNK_VERIFY_SSL=false` for
  dev. Set `true` (or a CA-bundle path) in production.
- **GateGuard "Fact-Forcing Gate"** blocks the *first* Bash command each session — state the
  request + what the command does, then re-run the same command.
- **HEC indexing lag.** After `populate`, allow ~4 s before re-searching (already handled in
  scripted mode via a `time.sleep(4)`).
- **Search-window stacking.** Injected events live in the 15-min window, so repeated fires
  accumulate. For a clean single-incident screenshot, clear the index first (the `| delete`
  one-liner in §3). The admin Bearer token has the capability; it's config-safe (no index or
  HEC teardown).
- **Windows console mojibake.** Em-dashes can render as `â€"`/`�` when JSON is piped through
  the cp1252 console. The stored email is correct UTF-8 — verify via the mailpit API/UI, not
  the piped console echo.

---

## 6. Where the polish lives

The "boss-impressive" report is two files, both pure logic (no infra to change):

- **`triage/report.py`** — string-builds the HTML/plain-text report: colour-coded severity
  header, error-code/service tables with inline bar charts, region chips, latency, root-cause
  hypothesis, recommended actions, and the Splunk deep-link button.
- **`triage/tools.py`** → `_generate_events()` — produces **realistically skewed** synthetic
  data (one dominant service + error code + region, with latency tracking the failure mode)
  via deterministic weighted distributions, so the incident reads like a real outage rather
  than a flat round-robin. Per-scenario weights are in `_SCENARIOS`.
