#!/bin/sh
set -e
mkdir -p /run/vpn-exit-node /var/run/tailscale /var/lib/vpn-exit-node

# tailscaled marks its own packets (0x80000), and its rules send them to the main table,
# where the VPN's routes are. A VPN that claims private ranges, such as 192.168/16, would
# then take Tailscale's direct connections to devices on the host's LAN, whatever that
# LAN is. So route marked packets out of the container's own network first.
gateway=$(ip route show default dev eth0 | awk '{print $3}')
ip route replace default via "$gateway" dev eth0 onlink table 101
ip rule add fwmark 0x80000/0xff0000 lookup 101 priority 5200 2>/dev/null || true

# With TS_LAN_ADDRESS, also announce the Docker host's LAN address with tailscaled's
# port, so devices on the LAN can connect directly (README.md).
if [ -n "$TS_LAN_ADDRESS" ]; then
  export TS_DEBUG_PRETENDPOINT="$TS_LAN_ADDRESS:${TS_PORT:-41642}"
fi
tailscaled --state=/var/lib/tailscale/tailscaled.state --tun=tailscale0 --port="${TS_PORT:-41642}" &
until [ -S /var/run/tailscale/tailscaled.sock ]; do sleep 0.5; done

# On the first start this logs a login URL; after that it returns straight away.
# --accept-dns=false keeps /etc/resolv.conf for the VPN's DNS servers.
tailscale up --reset --hostname="$TS_HOSTNAME" --advertise-exit-node --accept-dns=false \
  ${TS_AUTHKEY:+--auth-key="$TS_AUTHKEY"} &

# The sign-in browser's screen, viewable at http://<node>:6080/vnc.html
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
until [ -e /tmp/.X11-unix/X99 ]; do sleep 0.2; done
x11vnc -display :99 -localhost -rfbport 5900 -forever -shared -nopw -quiet &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

export DISPLAY=:99
exec /opt/venv/bin/python /usr/local/bin/vpn-exit-node-web
