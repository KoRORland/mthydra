#!/bin/bash
# Integration MVP — a real 3-node fleet in containers, walking the quickstart:
#
#   mt-controller  (EU controller)  ── quickstart Parts 3,5,6,7
#   mt-vantage     (RU probe vantage) ── Part 5, runs the openssl probe
#   mt-rubox       (RU box)          ── Part 7, runs the real agent + tunnel
#   mt-minio       (S3 backend)      ── stands in for AWS S3 (real boto3 path)
#
# Everything is the real mthydra code; only the 3 external SaaS sinks
# (Telegram/email/AWS) are stood in for locally. Verifies the mtg tunnel is up.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
NET=mtnet
KEY=mthydra
SECRET=mthydra-secret-key-0123456789
BUCKET=mthydra-state
COVER=www.cloudflare.com
VANTAGE_LABEL=ru-msk-1
IMAGE_VERSION=iv-harness
MTG_VER="${MTG_VER:-2.2.8}"
MTG_TARBALL="$REPO/harness/agent-boot/.cache/mtg-$MTG_VER-linux-amd64.tar.gz"

C_MINIO=mt-minio; C_CTRL=mt-controller; C_VANT=mt-vantage; C_RUBOX=mt-rubox

step()  { echo; echo "============================================================"; \
          echo "== $*"; echo "============================================================"; }
note()  { echo "  -> $*"; }

cleanup() {
    docker rm -f "$C_MINIO" "$C_CTRL" "$C_VANT" "$C_RUBOX" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------- preflight
command -v docker >/dev/null || { echo "docker not found"; exit 2; }
[ "$(uname -m)" = "x86_64" ] || echo "warn: host $(uname -m) (mtg ELF is amd64)"
if [ ! -f "$MTG_TARBALL" ]; then
    note "downloading mtg $MTG_VER"
    mkdir -p "$(dirname "$MTG_TARBALL")"
    curl -fsSL -o "$MTG_TARBALL" \
      "https://github.com/9seconds/mtg/releases/download/v$MTG_VER/mtg-$MTG_VER-linux-amd64.tar.gz"
fi

step "0. clean slate + network + image builds"
cleanup
docker network create "$NET" >/dev/null
note "building controller image (installs real mthydra source)…"
docker build -q -f "$HERE/controller.Dockerfile" -t mt-controller-img "$REPO" >/dev/null
note "building vantage image…"
docker build -q -f "$HERE/vantage.Dockerfile" -t mt-vantage-img "$HERE" >/dev/null
note "building rubox image (amd64; installs sing-box)…"
docker build -q --platform linux/amd64 -f "$HERE/rubox.Dockerfile" -t mt-rubox-img "$REPO" >/dev/null

step "1. S3 backend (MinIO) + locked bucket"
docker run -d --name "$C_MINIO" --network "$NET" --network-alias minio \
    -e MINIO_ROOT_USER="$KEY" -e MINIO_ROOT_PASSWORD="$SECRET" \
    minio/minio server /data >/dev/null
note "waiting for MinIO…"
for i in $(seq 1 30); do
    docker run --rm --network "$NET" --entrypoint sh minio/mc -c \
        "mc alias set h http://minio:9000 $KEY $SECRET" >/dev/null 2>&1 && break
    sleep 1
done
docker run --rm --network "$NET" --entrypoint sh minio/mc -c \
    "mc alias set h http://minio:9000 $KEY $SECRET && \
     mc mb --with-lock --ignore-existing h/$BUCKET && \
     mc version enable h/$BUCKET && mc ls h" >/dev/null
note "bucket $BUCKET created (versioning + object-lock)"

step "2. EU controller — run the quickstart install steps"
docker run -d --name "$C_CTRL" --network "$NET" --network-alias controller \
    mt-controller-img >/dev/null
# docker cp (not -v) to sidestep SELinux relabeling of the host tarball.
docker cp "$MTG_TARBALL" "$C_CTRL":/opt/mtg.tar.gz
docker exec \
    -e S3_ENDPOINT="http://minio:9000" -e S3_BUCKET="$BUCKET" -e S3_KEY_ID="$KEY" \
    -e B2_APPLICATION_KEY="$SECRET" -e COVER_DOMAIN="$COVER" \
    -e VANTAGE_LABEL="$VANTAGE_LABEL" -e IMAGE_VERSION="$IMAGE_VERSION" \
    -e MTG_TARBALL=/opt/mtg.tar.gz \
    "$C_CTRL" bash /opt/quickstart.sh
BOX_ID="$(docker exec "$C_CTRL" cat /tmp/box_id.txt)"
note "minted box_id=$BOX_ID"

step "3. RU probe vantage — bring the host up (Part 5)"
docker run -d --name "$C_VANT" --network "$NET" --network-alias vantage \
    mt-vantage-img >/dev/null
note "vantage up (openssl/ncat present; runs probes manually per quickstart §5)"

step "4. RU box — boot the agent from the minted seed (Part 7)"
docker cp "$C_CTRL":/tmp/seed.json "$HERE/.seed.json"
docker create --name "$C_RUBOX" --network "$NET" --network-alias rubox \
    --platform linux/amd64 --privileged mt-rubox-img >/dev/null
docker cp "$HERE/.seed.json" "$C_RUBOX":/opt/seed.json
rm -f "$HERE/.seed.json"
docker start "$C_RUBOX" >/dev/null
note "rubox starting; polling for tunnel (mtg + sing-box on :443)…"
TUNNEL_UP=""
for i in $(seq 1 80); do
    if docker logs "$C_RUBOX" 2>&1 | grep -q "PASS: mtg + sing-box"; then
        TUNNEL_UP=1; break
    fi
    if docker logs "$C_RUBOX" 2>&1 | grep -q "\[rubox\] FAIL:"; then
        break
    fi
    sleep 2
done

step "5. VERIFY — is the tunnel running?"
echo "--- rubox: listeners + processes ---"
docker exec "$C_RUBOX" sh -c 'ss -tlnp 2>/dev/null | grep ":443 " || echo "(no :443 listener)"' || true
docker exec "$C_RUBOX" sh -c 'pgrep -ax mtg; pgrep -ax sing-box' || true
echo
echo "--- rubox: agent debug log (debug mode was enabled at boot) ---"
docker exec "$C_RUBOX" sh -c 'sed -n "1,20p" /run/mthydra/debug/agent-debug.log 2>/dev/null || echo "(no debug log)"' || true
echo
echo "--- vantage -> rubox:443 TLS handshake (the RU-vantage probe, §6.1/§when-wrong) ---"
docker exec "$C_VANT" sh -c \
    "openssl s_client -connect rubox:443 -servername $COVER </dev/null 2>/dev/null | \
     grep -E 'CONNECTED|Cipher is|Verify return' | head -5 || echo 'handshake failed'" || true
echo
RUBOX_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$C_RUBOX")"
echo "--- controller: mark box live @ $RUBOX_IP + list fleet ---"
docker exec "$C_CTRL" mthydra-ops ru-bringup --box-id "$BOX_ID" \
    --public-ip "$RUBOX_IP" --non-interactive || true
docker exec "$C_CTRL" mthydra-controller ru-box-list --json \
    --db-path /var/lib/mthydra/state.sqlite || true

step "RESULT"
if [ -n "$TUNNEL_UP" ]; then
    echo "✅ TUNNEL UP — mtg + sing-box stable, listening on :443; reachable from the RU vantage."
    echo "   Containers left running for inspection:"
    echo "     docker logs $C_RUBOX        # full agent boot + tunnel log"
    echo "     docker exec -it $C_RUBOX ss -tlnp"
    echo "   Tear down with: docker rm -f $C_MINIO $C_CTRL $C_VANT $C_RUBOX && docker network rm $NET"
    exit 0
else
    echo "❌ TUNNEL DID NOT COME UP — full rubox boot log:"
    docker logs "$C_RUBOX" 2>&1 | tail -80
    exit 1
fi
