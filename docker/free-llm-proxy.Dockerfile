# free-llm-proxy-router sidecar (kaiser-data/free-llm-proxy-router, MIT).
# Builds the Go proxy + scan binaries and serves an OpenAI-compatible
# endpoint that fails over across free-tier providers (Groq, Gemini,
# OpenRouter, Cerebras, HuggingFace, ...). No request can bill you:
# every upstream is a recharging free tier.
FROM golang:1.23-bookworm AS build

RUN apt-get update && apt-get install -y --no-install-recommends git make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 https://github.com/kaiser-data/free-llm-proxy-router.git .

RUN make build-proxy build-scan

FROM debian:12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=build /src/bin/free-llm-proxy /usr/local/bin/free-llm-proxy
COPY --from=build /src/bin/free-llm-scan /usr/local/bin/free-llm-scan
COPY docker/free-llm-proxy-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER appuser
ENV HOME=/home/appuser
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
