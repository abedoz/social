#!/bin/sh

echo "=== Starting Tailscale ==="

# Start tailscaled in userspace mode with a local SOCKS5 proxy
tailscaled --tun=userspace-networking \
           --socks5-server=localhost:1055 \
           --state=/var/lib/tailscale/tailscaled.state &

sleep 5

# Connect to tailnet and route all traffic through the exit node
if [ -n "$TAILSCALE_AUTHKEY" ]; then
    if [ -n "$TAILSCALE_EXIT_NODE" ]; then
        tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=railway-bot --exit-node="$TAILSCALE_EXIT_NODE" && \
            echo "Tailscale connected with exit node: $TAILSCALE_EXIT_NODE" || \
            echo "WARNING: Tailscale failed to connect. Bot will run without VPN."
    else
        tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=railway-bot && \
            echo "Tailscale connected (no exit node)." || \
            echo "WARNING: Tailscale failed to connect. Bot will run without VPN."
    fi
    tailscale status 2>&1 || true
else
    echo "WARNING: TAILSCALE_AUTHKEY not set. Skipping Tailscale. Bot will run without VPN."
fi

echo "=== Starting Bot ==="
exec python bot.py
