#!/bin/sh
set -e

# Start tailscaled in userspace mode with a local SOCKS5 proxy
tailscaled --tun=userspace-networking \
           --socks5-server=localhost:1055 \
           --state=/var/lib/tailscale/tailscaled.state &

sleep 3

# Connect to tailnet using the auth key
tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=railway-bot

echo "Tailscale connected."
tailscale status

# Start the bot
exec python bot.py
