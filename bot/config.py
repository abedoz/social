import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN environment variable is required. Set it in .env or export it.")
    sys.exit(1)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/tgbot_downloads")
Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))

# Proxy for yt-dlp — defaults to Tailscale's local SOCKS5 proxy
PROXY = os.getenv("PROXY", "socks5://localhost:1055")

# Cookie file path (decoded from COOKIES_B64 by start.sh)
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
