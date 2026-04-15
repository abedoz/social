#!/bin/sh

echo "=== Starting Tailscale ==="

# Start tailscaled with both SOCKS5 and HTTP proxy
tailscaled --tun=userspace-networking \
           --socks5-server=localhost:1055 \
           --outbound-http-proxy-listen=localhost:1056 \
           --state=/var/lib/tailscale/tailscaled.state &

sleep 5

if [ -n "$TAILSCALE_AUTHKEY" ]; then
    echo "TAILSCALE_EXIT_NODE: ${TAILSCALE_EXIT_NODE:-not set}"

    tailscale up \
        --authkey="$TAILSCALE_AUTHKEY" \
        --hostname=railway-bot \
        --accept-routes \
        --exit-node="${TAILSCALE_EXIT_NODE:-}" 2>&1
    echo "tailscale up exit code: $?"

    sleep 3
    tailscale status 2>&1 || true

    # Test connectivity through both proxies
    echo "--- Proxy connectivity test ---"
    echo -n "Direct IP:     " && curl -s --max-time 10 https://api.ipify.org || echo "failed"
    echo ""
    echo -n "SOCKS5 proxy:  " && curl -s --max-time 10 --proxy socks5://localhost:1055 https://api.ipify.org || echo "failed"
    echo ""
    echo -n "HTTP proxy:    " && curl -s --max-time 10 --proxy http://localhost:1056 https://api.ipify.org || echo "failed"
    echo ""
else
    echo "WARNING: TAILSCALE_AUTHKEY not set. Skipping Tailscale."
fi

echo "=== Starting Bot ==="
exec python bot.py
