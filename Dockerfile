# syntax=docker/dockerfile:1
FROM python:3.13-slim

# Release version injected by the build workflow (from the git tag). Defaults to the in-tree
# placeholder so local `docker build` still works.
ARG VERSION=0.0.0

# Container defaults, overridable with `docker run -e ...`. HTTP_PORT defaults to 80 because the
# Ambilight TVs assume the Hue bridge is there; the Home Assistant add-on overrides these from its
# options. Also settable: HTTPS_PORT, LOG_LEVEL, LOG_FILE.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTTP_PORT=80 \
    LOG_LEVEL=info

WORKDIR /app
COPY . /app

# Stamp the release version into the package metadata, then install. cryptography/aiohttp ship
# wheels for the targeted arches (amd64/arm64), so no compiler toolchain is needed here.
RUN sed -i "s/^version = .*/version = \"${VERSION}\"/" pyproject.toml \
    && pip install --no-cache-dir . \
    && rm -rf /root/.cache

# Persistent config + state. config.yaml (bridge credentials) and bridge.log live here. Under
# Home Assistant this is the add-on's automatic persistent /data mount.
VOLUME ["/data"]

# Informational only — the bridge needs host networking (SSDP/mDNS multicast + the virtual
# bridge on the LAN), where published ports are ignored. Hue API + web UI on 80, SSDP on
# 1900/udp, inbound entertainment DTLS on 2100/udp, mDNS on 5353/udp. (TLS is off by default;
# append --https-port 443 to the entrypoint, and EXPOSE 443, only if a client needs it.)
EXPOSE 80 1900/udp 2100/udp 5353/udp

# Ports + log level come from the env above (HTTP_PORT, ...) or, under HA, the add-on options.
ENTRYPOINT ["ambilight-hue-bridge", "--data-dir", "/data"]
