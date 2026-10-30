# The base image is multi-architecture; the build's platform picks amd64 or aarch64.
FROM ghcr.io/home-assistant/base:3.24

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# The relay is the Python standard library; nothing is pip-installed. The base
# tag fixes the Alpine branch; an exact apk pin breaks the rebuild once Alpine
# replaces that package version within it.
# hadolint ignore=DL3018
RUN apk add --no-cache python3 ca-certificates \
 && python3 -c "import http.server, sqlite3, ssl, urllib.request; print('python ok')"

WORKDIR /opt/gmc_relay
COPY server.py panel.html ./
RUN python3 -m py_compile server.py

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1

# The Supervisor restarts the app when this fails; /healthz is the port 80 listener's.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1

CMD ["/run.sh"]

ARG BUILD_VERSION
LABEL \
    maintainer="Lewis Heidrick" \
    org.opencontainers.image.title="GMC Relay" \
    org.opencontainers.image.description="Receive a GQ GMC Geiger counter's WiFi upload, hand it to Home Assistant, forward it to gmcmap.com" \
    org.opencontainers.image.vendor="Lewis Heidrick" \
    org.opencontainers.image.authors="Lewis Heidrick" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/heidrickla/ha-gmc-relay" \
    org.opencontainers.image.source="https://github.com/heidrickla/ha-gmc-relay" \
    org.opencontainers.image.documentation="https://github.com/heidrickla/ha-gmc-relay/blob/main/gmc_relay/DOCS.md" \
    org.opencontainers.image.version="${BUILD_VERSION}"
