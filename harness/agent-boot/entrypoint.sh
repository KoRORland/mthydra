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

LOG=/var/log/agent.log

# A child that parses its config but immediately exits (e.g. sing-box FATAL) gets
# relaunched by the supervisor, so a bare pgrep can catch it mid-restart and
# false-PASS. Treat any FATAL / crash-loop / terminate line as a hard failure,
# and require the steady state to HOLD for a sustained window.
crashing() {
    grep -qE 'FATAL|crashed [0-9]+ times|TERMINATING' "$LOG" \
        || [ -f /run/mthydra/.shutdown-called ] || [ -f /tmp/.shutdown-called ]
}
steady() {
    pgrep -x mtg >/dev/null 2>&1 \
        && pgrep -x sing-box >/dev/null 2>&1 \
        && ss -tlnp 2>/dev/null | grep -q ':443 '
}
fail() {
    echo "==== agent.log ===="; sed -n '1,120p' "$LOG"
    echo "==== listeners ===="; ss -tlnp 2>/dev/null || true
    echo "[harness] FAIL: $1"; exit "${2:-1}"
}

ok=""
deadline=$((SECONDS + 45))
while [ "$SECONDS" -lt "$deadline" ]; do
    crashing && fail "child crash-loop / shutdown (see FATAL/TERMINATING above)" 3
    if steady; then ok=1; break; fi
    sleep 2
done
[ -n "$ok" ] || fail "agent did not reach steady state within 45s"

# Stability hold: it must STAY up (not a restart blip) and stay crash-free.
echo "[harness] steady state reached; holding 8s to confirm stability"
sleep 8
crashing && fail "child crash-loop after reaching steady state" 3
steady   || fail "steady state did not hold (child exited)"

echo "==== agent.log ===="; sed -n '1,120p' "$LOG"
echo "==== listeners ===="; ss -tlnp 2>/dev/null || true
echo "[harness] PASS: mtg + sing-box stable, mtg listening on :443"
exit 0
