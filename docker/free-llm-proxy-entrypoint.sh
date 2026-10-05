#!/bin/sh
# Entrypoint for the free-llm-proxy-router sidecar.
# Keys arrive as container env (first match wins in the proxy, so real env
# vars are picked up directly). Then refresh the free-model catalog
# (best-effort: serve even if the scan fails) and start the proxy.
set -eu

export HOME="${HOME:-/home/appuser}"
PROXY_HOME="$HOME/.free-llm-proxy-router"
mkdir -p "$PROXY_HOME"

if [ "${FREE_LLM_SKIP_SCAN:-0}" != "1" ]; then
  echo "[free-llm-proxy] refreshing free-model catalog (best-effort)..."
  free-llm-scan update || echo "[free-llm-proxy] catalog scan failed, serving with cached catalog"
else
  echo "[free-llm-proxy] FREE_LLM_SKIP_SCAN=1, skipping catalog scan"
fi

echo "[free-llm-proxy] listening on :8080"
exec free-llm-proxy
