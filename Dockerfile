FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OC_MCP_STATE_DIR=/var/lib/oc-mcp

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && install -d -m 0700 -o 10001 -g 10001 "$OC_MCP_STATE_DIR"
COPY bridge.py relay.py credentials.py setup_web.py setup.html oc-mcp.lua ./

USER 10001:10001
CMD ["python", "bridge.py", "serve", "--host", "0.0.0.0"]
