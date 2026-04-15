#!/bin/sh

echo "=== Starting Tailscale ==="

# Start tailscaled in userspace mode with a local SOCKS5 proxy
tailscaled --tun=userspace-networking \
           --socks5-server=localhost:1055 \
           --state=/var/lib/tailscale/tailscaled.state &

sleep 5

# Connect to tailnet and route all traffic through the exit node
if [ -n "$TAILSCALE_AUTHKEY" ]; then
    echo "TAILSCALE_AUTHKEY is set (length: $(printf '%s' "$TAILSCALE_AUTHKEY" | wc -c))"
    echo "TAILSCALE_EXIT_NODE: ${TAILSCALE_EXIT_NODE:-not set}"

    # First, just connect to tailnet (no exit node yet)
    echo "Attempting: tailscale up --authkey=*** --hostname=railway-bot"
    tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=railway-bot --accept-routes 2>&1
    UP_EXIT=$?
    echo "tailscale up exit code: $UP_EXIT"

    if [ $UP_EXIT -eq 0 ]; then
        echo "Tailscale authenticated."
        tailscale status 2>&1 || true

        # Now set exit node separately if configured
        if [ -n "$TAILSCALE_EXIT_NODE" ]; then
            echo "Setting exit node to: $TAILSCALE_EXIT_NODE"
            tailscale set --exit-node="$TAILSCALE_EXIT_NODE" 2>&1
            SET_EXIT=$?
            echo "tailscale set exit-node exit code: $SET_EXIT"
        fi
    else
        echo "WARNING: Tailscale failed to authenticate. Bot will run without VPN."
        echo "Check that TAILSCALE_AUTHKEY is valid and reusable."
    fi
else
    echo "WARNING: TAILSCALE_AUTHKEY not set. Skipping Tailscale. Bot will run without VPN."
fi

echo "=== Starting Bot ==="
exec python bot.py
