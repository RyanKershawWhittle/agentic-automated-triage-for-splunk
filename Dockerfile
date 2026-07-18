# One image, two roles (set via the compose `command`):
#   python -m triage.webhook       -> FastAPI webhook + orchestrator
#   python -m triage.mcp_server    -> MCP server (the three tools)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# curl is used by the compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY triage/ ./triage/
COPY scripts/ ./scripts/

# Default role; docker-compose overrides per service.
CMD ["python", "-m", "triage.webhook"]
