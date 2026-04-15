#!/bin/sh

echo "=== Starting Tailscale ==="

# Start tailscaled in userspace mode with a local SOCKS5 proxy
tailscaled --tun=userspace-networking \
           --socks5-server=localhost:1055 \
           --state=/var/lib/tailscale/tailscaled.state &

sleep 5

if [ -n "$TAILSCALE_AUTHKEY" ]; then
    echo "TAILSCALE_EXIT_NODE: ${TAILSCALE_EXIT_NODE:-not set}"

    # Connect + set exit node in a single command
    tailscale up \
        --authkey="$TAILSCALE_AUTHKEY" \
        --hostname=railway-bot \
        --accept-routes \
        --exit-node="${TAILSCALE_EXIT_NODE:-}" 2>&1
    echo "tailscale up exit code: $?"

    sleep 2
    tailscale status 2>&1 || true

    # Verify exit node is active
    echo "--- Exit node check ---"
    tailscale exit-node status 2>&1 || tailscale status --json 2>&1 | grep -i exit || echo "Could not verify exit node"
else
    echo "WARNING: TAILSCALE_AUTHKEY not set. Skipping Tailscale."
fi

echo "=== Starting Bot ==="
exec python bot.py
