#!/usr/bin/env bash
# End-to-end check against a live Immich: upload one fresh dual-file live photo
# (same stem, .jpg + .mp4) and assert that Immich ends up with the image linked
# and the clip hidden.
#
# The clip content is randomised so every run produces a new checksum; without
# that Immich would deduplicate the upload against a previous run and no
# AssetCreate event would fire.
#
# Usage:
#   IMMICH_VIVO_PASSWORD=... ./tools/test_e2e_immich.sh
#
# Env:
#   IMMICH_URL            default http://localhost:2283
#   COMPOSE_DIR           default ~/hermes-workspace/projects/immich
#   OWNER_EMAIL           default vivocamera@duruonanni.net
#   WAIT_SECONDS          default 90
set -euo pipefail

BASE="${IMMICH_URL:-http://localhost:2283}"
COMPOSE="${COMPOSE_DIR:-$HOME/hermes-workspace/projects/immich}"
OWNER_EMAIL="${OWNER_EMAIL:-vivocamera@duruonanni.net}"
WAIT_SECONDS="${WAIT_SECONDS:-90}"
: "${IMMICH_VIVO_PASSWORD:?set IMMICH_VIVO_PASSWORD}"
command -v ffmpeg >/dev/null || { echo "ffmpeg not found"; exit 2; }

WORK="$(mktemp -d)"
STEM="E2E_$(date +%Y%m%d_%H%M%S)_$RANDOM"
HUE="$(printf '%06x' $((RANDOM * RANDOM % 16777216)))"
STAMP="2026-01-01T00:00:00.000Z"
trap 'rm -rf "$WORK"' EXIT

echo "[*] generating $STEM (colour #$HUE)"
ffmpeg -y -loglevel error -f lavfi -i "color=c=0x$HUE:size=640x480:rate=30:duration=2" \
  -frames:v 1 -q:v 3 "$WORK/$STEM.jpg"
ffmpeg -y -loglevel error -f lavfi -i "color=c=0x$HUE:size=640x480:rate=30:duration=2" \
  -f lavfi -i "sine=frequency=440:duration=2" -shortest \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart "$WORK/$STEM.mp4"

TOKEN="$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$IMMICH_VIVO_PASSWORD\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["accessToken"])')" \
  || { echo "[!] login failed"; exit 1; }

upload() {
  curl -s -X POST "$BASE/api/assets" -H "Authorization: Bearer $TOKEN" \
    -F "assetData=@$1" \
    -F "deviceAssetId=$STEM-$(basename "$1")" \
    -F "deviceId=motion-photo-bridge-e2e" \
    -F "fileCreatedAt=$STAMP" -F "fileModifiedAt=$STAMP" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id", d))'
}

IMG_ID="$(upload "$WORK/$STEM.jpg")"
VID_ID="$(upload "$WORK/$STEM.mp4")"
echo "[*] uploaded image=$IMG_ID video=$VID_ID"

echo "[*] waiting up to ${WAIT_SECONDS}s for the workflow + webhook to link them"
STATE=""
for _ in $(seq 1 $((WAIT_SECONDS / 5))); do
  STATE="$(docker compose -f "$COMPOSE/docker-compose.yml" exec -T database \
    psql -U postgres immich -t -A -F'|' -c \
    "SELECT COALESCE(i.\"livePhotoVideoId\"::text,'NULL'), v.visibility
     FROM asset i JOIN asset v ON v.id='$VID_ID' WHERE i.id='$IMG_ID';" 2>/dev/null | tr -d ' \r')"
  [ "$STATE" = "$VID_ID|hidden" ] && break
  sleep 5
done

echo "[*] result: linked=${STATE%%|*} clip=${STATE##*|}"

# Clean up regardless of outcome: the test assets go to the Immich trash.
curl -s -o /dev/null -X DELETE "$BASE/api/assets" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d "{\"ids\":[\"$IMG_ID\",\"$VID_ID\"]}" || true

if [ "$STATE" = "$VID_ID|hidden" ]; then
  echo "[+] PASS — image linked to its clip and the clip is hidden"
  exit 0
fi
echo "[-] FAIL — expected linked=$VID_ID clip=hidden, got '${STATE}'"
exit 1
