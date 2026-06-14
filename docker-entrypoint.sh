#!/bin/sh
set -e

# Unraid-style user mapping. The container starts as root, then drops to
# PUID:PGID (default 99:100 = nobody:users on Unraid) so dummy files written to
# your media share are owned like the rest of the array, not root:root.
PUID="${PUID:-99}"
PGID="${PGID:-100}"
umask "${UMASK:-022}"

CONFIG="${MS_CONFIG:-/config/config.yaml}"
mkdir -p "$(dirname "$CONFIG")"

# On first run there is no config yet — seed one from the bundled example so the
# container boots and the dashboard/Settings page is reachable to finish setup.
if [ ! -f "$CONFIG" ]; then
    echo "[mediaspektor] No config at $CONFIG — seeding from config.yaml.example"
    cp /app/config.yaml.example "$CONFIG"
fi

# The persisted config dir should be owned by the user we drop to.
chown -R "$PUID:$PGID" /config 2>/dev/null || true

echo "[mediaspektor] starting as ${PUID}:${PGID}"
export HOME=/config
exec gosu "${PUID}:${PGID}" python3 mediaspektor.py --host 0.0.0.0 --port 5000 --config "$CONFIG"
