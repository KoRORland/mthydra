#!/bin/bash
# RU box boot — replays the cloud-init bundle minted by the controller's
# ru-bringup (bootcmd host-prep + runcmd agent fetch), runs the REAL agent with
# debug enabled, and polls for the tunnel (mtg + sing-box, :443) to come up.
#
# The seed.json (minted by the controller) is bind-mounted at /opt/seed.json.
# The agent source tarball + the mtg ELF are pulled from MinIO at boot via the
# presigned URLs embedded in the seed — exactly as a real TimeWeb VM would.
set -u

echo "[rubox] === bootcmd: host prep (mirrors cloud-init) ==="
swapoff -a 2>/dev/null || true
mkdir -p /run/mthydra /var/log
# Dedicated EXEC tmpfs at /run/mthydra (/run is noexec; mtg must exec) + /var/log.
mount -t tmpfs -o nosuid,nodev,mode=0700 tmpfs /run/mthydra
mount -t tmpfs tmpfs /var/log
# kernel.core_pattern is host-global; isolate via bind mount so apply/verify
# never touches the host value.
printf '|/bin/false' > /tmp/core_pattern
mount --bind /tmp/core_pattern /proc/sys/kernel/core_pattern 2>/dev/null \
    || echo "[rubox] warn: could not bind-mount core_pattern (need --privileged)"

echo "[rubox] === seed (minted by controller provision-seed) ==="
cp /opt/seed.json /run/mthydra/seed.json
chmod 0600 /run/mthydra/seed.json
AGENT_URL="$(python3 -c 'import json,sys;print(json.load(open("/opt/seed.json"))["agent_source_url"])')"
AGENT_SHA="$(python3 -c 'import json,sys;print(json.load(open("/opt/seed.json"))["agent_source_sha256"])')"
IMG_URL="$(python3 -c 'import json,sys;print(json.load(open("/opt/seed.json"))["image"]["url"])')"
echo "[rubox] agent_source_url = ${AGENT_URL%%\?*}"
echo "[rubox] image.url        = ${IMG_URL%%\?*}"

echo "[rubox] === runcmd: fetch + verify agent tarball from S3 ==="
curl -fsSL "$AGENT_URL" -o /run/mthydra/agent.tar.gz || {
    echo "[rubox] FAIL: could not download agent tarball"; exit 10; }
echo "${AGENT_SHA}  /run/mthydra/agent.tar.gz" | sha256sum -c - || {
    echo "[rubox] FAIL: agent tarball sha256 mismatch"; exit 11; }
mkdir -p /run/mthydra/agent
tar -xzf /run/mthydra/agent.tar.gz -C /run/mthydra/agent

echo "[rubox] === debug enabled (touch /run/mthydra/debug.flag) ==="
# The agent's DebugPoller turns on verbose, UNREDACTED logging to
# /run/mthydra/debug/agent-debug.log within 5s of the flag appearing.
mkdir -p /run/mthydra/debug
touch /run/mthydra/debug.flag

echo "[rubox] === launching: python3 -m mthydra.ru_agent ==="
PYTHONPATH=/run/mthydra/agent python3 -m mthydra.ru_agent >/var/log/agent.log 2>&1 &

LOG=/var/log/agent.log
crashing() {
    grep -qE 'FATAL|crashed [0-9]+ times|TERMINATING' "$LOG" \
        || [ -f /run/mthydra/.shutdown-called ] || [ -f /tmp/.shutdown-called ]
}
steady() {
    pgrep -x mtg >/dev/null 2>&1 \
        && pgrep -x sing-box >/dev/null 2>&1 \
        && ss -tlnp 2>/dev/null | grep -q ':443 '
}
dump() {
    echo "==== agent.log ===="; sed -n '1,160p' "$LOG"
    echo "==== debug log (/run/mthydra/debug/agent-debug.log) ===="
    sed -n '1,80p' /run/mthydra/debug/agent-debug.log 2>/dev/null || echo "(none yet)"
    echo "==== listeners ===="; ss -tlnp 2>/dev/null || true
}
fail() { dump; echo "[rubox] FAIL: $1"; exit "${2:-1}"; }

ok=""
deadline=$((SECONDS + 90))
while [ "$SECONDS" -lt "$deadline" ]; do
    crashing && fail "child crash-loop / shutdown (see FATAL/TERMINATING above)" 3
    if steady; then ok=1; break; fi
    sleep 2
done
[ -n "$ok" ] || fail "agent did not reach steady state (mtg+sing-box+:443) within 90s"

echo "[rubox] steady state reached; holding 8s to confirm stability"
sleep 8
crashing && fail "child crash-loop after reaching steady state" 3
steady   || fail "steady state did not hold (child exited)"

dump
echo "[rubox] PASS: mtg + sing-box stable, mtg listening on :443 (tunnel UP)"
# Stay up so the vantage can probe :443 and the controller can mark-live.
echo "[rubox] holding open for external probes…"
exec sleep infinity
