ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.24
# Tag is in the ARG default and build.yaml; the Supervisor overrides BUILD_FROM
# per architecture, so it cannot be tagged inline.
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

ARG BUILD_ARCH=amd64

# The relay is the Python standard library; nothing is pip-installed. The base
# tag fixes the Alpine branch; an exact apk pin breaks the rebuild once Alpine
# replaces that package version within it.
# hadolint ignore=DL3018
RUN apk add --no-cache python3 ca-certificates \
 && python3 -c "import http.server, ssl, urllib.request; print('python ok')"

WORKDIR /opt/gmc_relay
COPY server.py ./
RUN python3 -m py_compile server.py

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1

CMD ["/run.sh"]

ARG BUILD_DATE
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_REF
ARG BUILD_REPOSITORY
ARG BUILD_VERSION
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    maintainer="Lewis Heidrick" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.vendor="Lewis Heidrick" \
    org.opencontainers.image.authors="Lewis Heidrick" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/heidrickla/ha-gmc-relay" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.documentation="https://github.com/${BUILD_REPOSITORY}/blob/main/gmc_relay/DOCS.md" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.revision="${BUILD_REF}" \
    org.opencontainers.image.version="${BUILD_VERSION}"
