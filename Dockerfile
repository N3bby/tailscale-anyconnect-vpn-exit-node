FROM tailscale/tailscale:stable AS tailscale

FROM debian:trixie-slim
# openconnect speaks AnyConnect. Chromium on a virtual display, viewed through noVNC,
# handles the single sign-on that Cisco's client does in its own browser window.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openconnect vpnc-scripts iptables iproute2 ca-certificates \
      python3 python3-venv chromium fonts-liberation xvfb x11vnc novnc websockify \
 && rm -rf /var/lib/apt/lists/*
# Playwright drives Debian's Chromium, so skip its own browser download.
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir playwright
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/
COPY entrypoint.sh vpn-script /usr/local/bin/
COPY web.py /usr/local/bin/vpn-exit-node-web
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
