# Verified from the official registry on 2026-10-24; this is the multiarch
# index digest for v3.41.3, not the moving latest tag.
FROM qmcgaw/gluetun:v3.41.3@sha256:fa19cc76b2af13d57a8d3dc3066f2ada061b1c761b8aecf989b3877c0486e027
RUN apk add --no-cache jq
COPY validate-options.jq /opt/surfshark-gateway/validate-options.jq
COPY --chmod=755 run.sh /opt/surfshark-gateway/run.sh
LABEL io.hass.name="Surfshark Gateway" \
      io.hass.description="Internal browser proxy through an isolated Surfshark VPN" \
      io.hass.type="addon" io.hass.arch="amd64" io.hass.version="0.1.0"
# Preserve Gluetun's inherited healthcheck: its endpoint stays at 127.0.0.1:9999.
ENTRYPOINT ["/opt/surfshark-gateway/run.sh"]
