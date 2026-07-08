# ── Multi-stage Dockerfile for OpenDesk Relay Server ──
# Build stage
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md MANIFEST.in ./
COPY src/ ./src/

RUN pip install --no-cache-dir build && \
    python -m build --wheel

# Runtime stage
FROM python:3.12-slim

LABEL org.opencontainers.image.title="OpenDesk Relay Server"
LABEL org.opencontainers.image.description="Standalone TCP relay server for OpenDesk"
LABEL org.opencontainers.image.source="https://github.com/opendesk/opendesk"

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Copy the built wheel from builder
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && \
    rm -rf /tmp/*.whl

# Create non-root user
RUN useradd -m -u 1000 relay && \
    mkdir -p /home/relay/.opendesk && \
    chown -R relay:relay /home/relay/.opendesk

USER relay
WORKDIR /home/relay

# Ports: relay (8474), dashboard (8484)
EXPOSE 8474 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8484/health')" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["relay-server", "--port", "8474"]
