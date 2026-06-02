"""mthydra RU agent — long-lived supervisor.

Reads /run/mthydra/seed.json, verifies it, downloads mtg, writes mtg and
sing-box configs, installs iptables rules, launches both children, runs
the descriptor refresh loop, terminates the box on persistent failure.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from mthydra.ru_agent import (
    binary,
    config_gen,
    descriptor_refresh,
    hardening,
    iptables,
    supervisor,
)
from mthydra.ru_agent import seed as seed_mod
from mthydra.ru_agent import shutdown as shutdown_mod

SEED_PATH = "/run/mthydra/seed.json"
MTG_PATH = "/run/mthydra/mtg"
MTG_CONFIG_PATH = "/run/mthydra/mtg.toml"
SING_BOX_CONFIG_PATH = "/run/mthydra/sing-box.json"
TPROXY_PORT = 12345

# Startup failures are usually transient at boot — the VM clock is still at epoch
# (so the mtg presigned download fails on TLS/expiry), the network/S3 isn't ready,
# or tmpfs isn't mounted yet. Retry a few times, then GIVE UP BY STAYING UP (exit
# non-zero, box remains reachable for diagnosis) — never `shutdown -h now` on a
# startup hiccup: cloud-init is once-per-instance and the seed lives on tmpfs, so
# a power-off comes back bare and unrecoverable. Fail-closed shutdown is reserved
# for *runtime* tamper (the periodic hardening-regression check).
STARTUP_MAX_ATTEMPTS = 10
STARTUP_RETRY_SECONDS = 15


class _StartupError(RuntimeError):
    """A startup step failed; treated as transient (retry), never power off."""


def _terminate(reason: str) -> None:
    shutdown_mod.terminate_box(reason)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Write `data` to `path` atomically.

    sing-box reads its config as a separate process and is told to reload via
    SIGHUP; a plain write_bytes() truncates-then-writes, so a concurrent read
    (or a crash mid-write) could see a partial/empty config. Write to a temp
    file in the same directory, fsync, then os.replace — readers always observe
    either the old file or the complete new one. (An in-process lock cannot
    protect a separate-process reader; atomic replace can.)
    """
    p = Path(path)
    tmp = p.with_name(f".{p.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)


def _startup():
    """Run the startup sequence once. Returns the loaded seed on success;
    raises _StartupError on any step failure (caller retries — never powers off
    the box). Hardening → seed → mtg binary → configs → iptables."""
    # 1. Hardening verification.
    try:
        hardening.verify_all()
    except hardening.HardeningError as e:
        raise _StartupError(f"hardening: {e}") from e

    # 2. Load + verify seed.
    try:
        s = seed_mod.load(SEED_PATH)
        seed_mod.verify_credential(s)
    except seed_mod.SeedError as e:
        raise _StartupError(f"seed: {e}") from e

    # 3. Download + verify mtg binary (the common transient: clock/network/S3).
    try:
        binary.download_and_verify(
            url=s.image["url"],
            expected_sha256=s.image["sha256"],
            out_path=MTG_PATH,
        )
    except binary.BinaryError as e:
        raise _StartupError(f"binary: {e}") from e

    # 4. Parse initial descriptor and render configs.
    import json
    import struct
    blob = s.initial_descriptor
    n = struct.unpack(">H", blob[:2])[0]
    descriptor_payload = json.loads(blob[2:2 + n])

    mtg_toml = config_gen.render_mtg_config(s, sing_box_socks_port=TPROXY_PORT)
    _atomic_write_bytes(MTG_CONFIG_PATH, mtg_toml)
    sing_box_json = config_gen.render_sing_box_config(
        s, descriptor_payload, tproxy_port=TPROXY_PORT,
    )
    _atomic_write_bytes(SING_BOX_CONFIG_PATH, sing_box_json)

    # 5. Install iptables rules.
    try:
        iptables.install(
            dc_cidrs_v4=s.telegram_dcs.get("v4", []),
            dc_cidrs_v6=s.telegram_dcs.get("v6", []),
            tproxy_port=TPROXY_PORT,
        )
    except iptables.IptablesError as e:
        raise _StartupError(f"iptables: {e}") from e
    return s


def main() -> int:
    # Startup with bounded retry. On persistent failure, STAY UP (return 2) for
    # diagnosis — do not power off (see STARTUP_MAX_ATTEMPTS comment).
    s = None
    for attempt in range(1, STARTUP_MAX_ATTEMPTS + 1):
        try:
            s = _startup()
            break
        except _StartupError as e:
            print(f"agent: startup failed (attempt {attempt}/"
                  f"{STARTUP_MAX_ATTEMPTS}): {e}", file=sys.stderr, flush=True)
            if attempt < STARTUP_MAX_ATTEMPTS:
                time.sleep(STARTUP_RETRY_SECONDS)
    if s is None:
        print("agent: startup did not succeed; staying up for diagnosis "
              "(box NOT powered off — `journalctl -u mthydra-agent` for details)",
              file=sys.stderr, flush=True)
        return 2

    # 6. Launch children.
    sup = supervisor.Supervisor(
        mtg_cmd=[MTG_PATH, "run", MTG_CONFIG_PATH],
        sing_box_cmd=["sing-box", "run", "-c", SING_BOX_CONFIG_PATH],
        on_persistent_failure=lambda r: _terminate(f"supervisor: {r}"),
    )
    sup.launch_all()

    # 7. Descriptor refresh loop on a background thread.
    def _rewrite(blob: bytes) -> None:
        import json
        import struct
        n = struct.unpack(">H", blob[:2])[0]
        payload = json.loads(blob[2:2 + n])
        new_json = config_gen.render_sing_box_config(
            s, payload, tproxy_port=TPROXY_PORT,
        )
        _atomic_write_bytes(SING_BOX_CONFIG_PATH, new_json)
        # SIGHUP via systemctl. For tests, this is mocked.
        import subprocess
        subprocess.run(["systemctl", "kill", "-s", "HUP", "mthydra-sing-box"])

    refresh = descriptor_refresh.RefreshLoop(
        url=s.descriptor_refresh_url,
        trust_anchors=list(s.descriptor_trust_anchors),
        initial_descriptor=s.initial_descriptor,
        rewrite_fn=_rewrite,
        terminate_fn=lambda r: _terminate(f"descriptor: {r}"),
    )
    threading.Thread(
        target=refresh.run_forever, daemon=True, name="descriptor-refresh",
    ).start()

    # 8. Periodic hardening + iptables re-verification loop.
    def _periodic_recheck():
        while True:
            time.sleep(15 * 60)  # 15 min
            try:
                hardening.verify_all()
            except hardening.HardeningError as e:
                _terminate(f"hardening regressed: {e}")
                return
            if not iptables.verify_installed(
                s.telegram_dcs.get("v4", []),
                s.telegram_dcs.get("v6", []),
                tproxy_port=TPROXY_PORT,
            ):
                # Re-install once; if that also fails next tick, terminate.
                try:
                    iptables.install(
                        dc_cidrs_v4=s.telegram_dcs.get("v4", []),
                        dc_cidrs_v6=s.telegram_dcs.get("v6", []),
                        tproxy_port=TPROXY_PORT,
                    )
                except iptables.IptablesError as e:
                    _terminate(f"iptables: {e}")
                    return

    threading.Thread(
        target=_periodic_recheck, daemon=True, name="periodic-recheck",
    ).start()

    # 9. Run supervisor in the main thread.
    sup.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
