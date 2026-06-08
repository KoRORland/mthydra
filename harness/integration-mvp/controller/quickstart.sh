#!/bin/bash
# EU controller — runs the real quickstart steps (Parts 3, 5, 6, 7) against the
# in-network MinIO S3 backend. Every mthydra-* call below is the real CLI; only
# the three external SaaS sinks (Telegram/email preflight) are skipped — they
# don't touch the tunnel and can't run without real accounts.
set -euo pipefail

DB=/var/lib/mthydra/state.sqlite
CONFIG=/etc/mthydra/controller.toml
: "${S3_ENDPOINT:?}" "${S3_BUCKET:?}" "${S3_KEY_ID:?}" "${B2_APPLICATION_KEY:?}"
: "${COVER_DOMAIN:?}" "${VANTAGE_LABEL:?}" "${IMAGE_VERSION:?}" "${MTG_TARBALL:?}"
export B2_APPLICATION_KEY

banner() { echo; echo "######## $* ########"; }

banner "Part 2.1 — operator age key (generated here for the harness)"
mkdir -p /root/.config/mthydra
if [ ! -f /root/.config/mthydra/operator.age ]; then
    age-keygen -o /root/.config/mthydra/operator.age 2>/dev/null
fi
AGE_RECIPIENT="$(grep '# public key:' /root/.config/mthydra/operator.age | awk '{print $4}')"
echo "age recipient: $AGE_RECIPIENT"

banner "Part 3 — install controller (bootstrap: init DB + controller.toml + authority)"
# Sink fields are required by the parser but only land in controller.toml; the
# harness never sends real alerts, so placeholders are fine.
mthydra-ops bootstrap \
    --age-recipient "$AGE_RECIPIENT" \
    --hostname controller \
    --operator-email ops@harness.invalid \
    --b2-key-id "$S3_KEY_ID" \
    --b2-bucket "$S3_BUCKET" \
    --b2-endpoint "$S3_ENDPOINT" \
    --obs-tg-bot-token "00000000:HARNESS_PLACEHOLDER_TOKEN_000000000000" \
    --obs-tg-chat-id 1 \
    --obs-smtp-host smtp.invalid --obs-smtp-from ops@harness.invalid \
    --obs-smtp-to ops@harness.invalid --obs-smtp-user ops@harness.invalid \
    --obs-smtp-pass placeholder \
    --dist-tg-bot-token "00000000:HARNESS_PLACEHOLDER_TOKEN_111111111111" \
    --dist-smtp-host smtp.invalid --dist-smtp-from ops@harness.invalid \
    --dist-smtp-user ops@harness.invalid --dist-smtp-pass placeholder

banner "Part 3 (first-descriptor) — register EU exit + sign descriptor"
python3 /opt/add_eu_exit.py "$DB" "$COVER_DOMAIN"

banner "Part 7.1 (image) — stage + promote mtg image into S3 (offline image-prepare)"
python3 /opt/stage_image.py "$MTG_TARBALL" "$IMAGE_VERSION" "$CONFIG" "$DB"
mthydra-controller image-current --json --db-path "$DB"

banner "backup-now — confirm the S3 backend really works (quickstart §3.4)"
mthydra-controller backup-now --db-path "$DB" --config "$CONFIG" --reason harness-validation

banner "Part 5 — register + attest the RU probe vantage"
mthydra-controller vantage-add "$VANTAGE_LABEL" \
    --label "$VANTAGE_LABEL" --source-kind cloud-cis --region-hint "RU-moscow" \
    --notes "harness vantage container" --db-path "$DB"
mthydra-controller vantage-attest-active "$VANTAGE_LABEL" \
    --evidence "openssl s_client baseline from $VANTAGE_LABEL (harness)" --db-path "$DB"
mthydra-controller vantage-list --db-path "$DB"

banner "Part 6 — register + attest the cover domain"
mthydra-controller cover-add "$COVER_DOMAIN" \
    --notes "harness candidate, tested from $VANTAGE_LABEL" --db-path "$DB"
mthydra-controller cover-attest-verified "$COVER_DOMAIN" \
    --vantage "$VANTAGE_LABEL" \
    --evidence "openssl s_client + curl -IL OK from $VANTAGE_LABEL (harness)" --db-path "$DB"
mthydra-controller cover-pool-stats --json --db-path "$DB"

banner "Part 7.1 — ru-bringup: mint box + cloud-init (publishes agent + descriptor to S3)"
# No --public-ip yet (the VM doesn't exist). Closing stdin makes the wizard defer
# after minting — it has already written the cloud-init bundle to /tmp.
set +e
mthydra-ops ru-bringup --provider timeweb --region "$VANTAGE_LABEL" </dev/null \
    | tee /tmp/ru-bringup.log
set -e
BOX_ID="$(grep -oE 'box_id=[A-Za-z0-9-]+' /tmp/ru-bringup.log | head -1 | cut -d= -f2)"
[ -n "$BOX_ID" ] || { echo "FAIL: no box_id minted"; exit 5; }
CLOUD_INIT="$(ls -t /tmp/ru-cloud-init-*.yaml | head -1)"
echo "minted box_id=$BOX_ID  cloud-init=$CLOUD_INIT"

banner "extract seed.json from the minted cloud-init bundle"
python3 /opt/extract_seed.py "$CLOUD_INIT" /tmp/seed.json
echo "$BOX_ID" > /tmp/box_id.txt
mthydra-controller ru-box-list --json --db-path "$DB"

banner "start the controller daemon (serve — backup orchestrator)"
nohup mthydra-controller serve --db-path "$DB" --config "$CONFIG" \
    >/var/log/serve.log 2>&1 &
sleep 2
echo "serve PID(s): $(pgrep -f 'mthydra-controller serve' | tr '\n' ' ')"
echo "QUICKSTART-OK box_id=$BOX_ID"
