# MediaSpektor — container image (intended to run on Unraid alongside Plex/Jellyfin/Emby)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MS_CONFIG=/config/config.yaml

WORKDIR /app

# gosu = drop root -> PUID/PGID at runtime; DejaVu fonts give the Pillow poster
# overlay a sane default when "Arial" isn't present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

# Config, SQLite DB and poster backups persist here — mount it as a volume
VOLUME ["/config"]
EXPOSE 5000

ENTRYPOINT ["./docker-entrypoint.sh"]
