FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    curl -fsSL https://tailscale.com/install.sh | sh && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ .
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
