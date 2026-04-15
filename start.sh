#!/bin/sh
set -e

# Start tailscaled in userspace mode with a local SOCKS5 proxy
tailscaled --tun=userspace-networking \
           --socks5-server=localhost:1055 \
           --state=/var/lib/tailscale/tailscaled.state &

sleep 3

# Connect to tailnet and route all traffic through the exit node
# Set TAILSCALE_EXIT_NODE in Railway env vars to your exit node's tailnet IP or hostname
# e.g. TAILSCALE_EXIT_NODE=gl-ax1800
if [ -n "$TAILSCALE_EXIT_NODE" ]; then
    tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=railway-bot --exit-node="$TAILSCALE_EXIT_NODE"
else
    tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=railway-bot
fi

echo "Tailscale connected."
tailscale status

# Start the bot
exec python bot.py
