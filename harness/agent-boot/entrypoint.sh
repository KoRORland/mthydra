#!/bin/bash
# In-container: replicate the RU-box host setup cloud-init does, then run the
# real agent and poll for a successful boot. Exit 0 on success, non-zero on any
# failure (with the agent log printed for diagnosis).
set -u

echo "[harness] host setup (mirrors cloud-init bootcmd)"
swapoff -a 2>/dev/null || true
mkdir -p /run/mthydra /var/log
# Dedicated EXEC tmpfs at /run/mthydra (/run is noexec; mtg must exec) + /var/log.
mount -t tmpfs -o nosuid,nodev,mode=0700 tmpfs /run/mthydra
mount -t tmpfs tmpfs /var/log
# kernel.core_pattern is host-global (not namespaced); isolate it with a bind
# mount so the agent's apply/verify never touches the host's value.
printf '|/bin/false' > /tmp/core_pattern
mount --bind /tmp/core_pattern /proc/sys/kernel/core_pattern 2>/dev/null \
    || echo "[harness] warn: could not bind-mount core_pattern (need --privileged)"

cp /opt/harness/seed.json /run/mthydra/seed.json
chmod 0600 /run/mthydra/seed.json

echo "[harness] launching: python3 -m mthydra.ru_agent"
python3 -m mthydra.ru_agent >/var/log/agent.log 2>&1 &

ok=""
deadline=$((SECONDS + 45))
while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -f /run/mthydra/.shutdown-called ] || [ -f /tmp/.shutdown-called ]; then
        echo "==== agent.log ===="; sed -n '1,120p' /var/log/agent.log
        echo "[harness] FAIL: agent attempted shutdown"; exit 3
    fi
    if pgrep -x mtg >/dev/null 2>&1 \
       && pgrep -x sing-box >/dev/null 2>&1 \
       && ss -tlnp 2>/dev/null | grep -q ':443 '; then
        ok=1; break
    fi
    sleep 2
done

echo "==== agent.log ===="; sed -n '1,120p' /var/log/agent.log
echo "==== listeners ===="; ss -tlnp 2>/dev/null || true
if [ -n "$ok" ]; then
    echo "[harness] PASS: mtg + sing-box up, mtg listening on :443"
    exit 0
fi
echo "[harness] FAIL: agent did not reach steady state within 45s"
exit 1
