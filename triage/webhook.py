"""FastAPI webhook listener — the pipeline's front door.

Splunk's webhook alert action POSTs here when the saved search fires. We normalise
the payload into an alert context and hand it to the orchestrator, which runs the
agentic triage (search -> maybe populate -> re-search -> summarise -> email).

Endpoints:
    GET  /health        liveness + config + Splunk/SMTP reachability
    POST /webhook       Splunk alert action target
    POST /test-triage   fire a synthetic alert (no Splunk alert needed) for quick demos
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .orchestrator import OrchestratorError, run_triage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("triage.webhook")

app = FastAPI(title="Alert-Triage Webhook", version="1.0.0")


def _normalise_alert(payload: dict) -> dict:
    """Map a Splunk webhook payload (or a hand-rolled test payload) into our context."""
    result = payload.get("result") or {}
    if not result and isinstance(payload.get("results"), list) and payload["results"]:
        result = payload["results"][0]
    return {
        "alert_name": payload.get("search_name") or payload.get("alert_name") or "Splunk Alert",
        "index": payload.get("index") or result.get("index") or config.TRIAGE_INDEX,
        "sourcetype": payload.get("sourcetype") or result.get("sourcetype") or config.TRIAGE_SOURCETYPE,
        "search": payload.get("search") or payload.get("search_query") or "(not supplied by Splunk webhook)",
        "earliest": payload.get("earliest", "-15m"),
        "latest": payload.get("latest", "now"),
        "results": payload.get("results") or ([result] if result else []),
        "results_link": payload.get("results_link", ""),
    }


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "alert-triage-webhook", "config": config.summary()})


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        raw = (await request.body()).decode("utf-8", "replace")
        log.warning("non-JSON webhook body: %s", raw[:200])
        return JSONResponse({"ok": False, "error": "expected JSON body"}, status_code=400)

    alert = _normalise_alert(payload)
    log.info("ALERT received: %s (index=%s)", alert["alert_name"], alert["index"])
    return _triage(alert)


@app.post("/test-triage")
async def test_triage(request: Request) -> JSONResponse:
    """Synthesise an alert for the configured index and run the full pipeline.

    Optional JSON body: {"index":..., "sourcetype":..., "alert_name":...}.
    Results are intentionally empty to exercise the populate-then-re-search path.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    alert = _normalise_alert({
        "search_name": body.get("alert_name", "Manual Test Alert"),
        "index": body.get("index", config.TRIAGE_INDEX),
        "sourcetype": body.get("sourcetype", config.TRIAGE_SOURCETYPE),
        "search": f"index={body.get('index', config.TRIAGE_INDEX)} level=ERROR | stats count",
        "results": [],
    })
    log.info("TEST triage requested for index=%s", alert["index"])
    return _triage(alert)


def _triage(alert: dict) -> JSONResponse:
    try:
        report = run_triage(alert)
    except OrchestratorError as e:
        log.error("orchestrator unavailable: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    except Exception as e:  # noqa: BLE001 — report any unexpected failure cleanly
        log.exception("triage failed")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)

    log.info("triage done: tools=%s email_sent=%s", report.get("tool_calls"), report.get("email_sent"))
    return JSONResponse(report)


def main() -> None:
    import uvicorn

    print(f"[webhook] Alert-Triage webhook on 0.0.0.0:{config.WEBHOOK_PORT}")
    print(f"[webhook] config: {config.summary()}")
    uvicorn.run(app, host="0.0.0.0", port=config.WEBHOOK_PORT, log_level="info")


if __name__ == "__main__":
    main()
