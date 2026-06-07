"""mthydra RU agent — long-lived supervisor.

Reads /run/mthydra/seed.json, verifies it, downloads mtg, writes mtg and
sing-box configs, installs iptables rules, launches both children, runs
the descriptor refresh loop, terminates the box on persistent failure.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from pathlib import Path

from mthydra.ru_agent import (
    binary,
    config_gen,
    descriptor_refresh,
    desync,
    hardening,
    iptables,
    supervisor,
    tunnel_check,
)
from mthydra.ru_agent import seed as seed_mod
from mthydra.ru_agent import shutdown as shutdown_mod

SEED_PATH = "/run/mthydra/seed.json"
MTG_PATH = "/run/mthydra/mtg"
MTG_CONFIG_PATH = "/run/mthydra/mtg.toml"
SING_BOX_CONFIG_PATH = "/run/mthydra/sing-box.json"
HEALTH_PATH = "/run/mthydra/health.json"
TPROXY_PORT = 12345
NFQWS_PATH = "/run/mthydra/nfqws"
DESYNC_QNUM = 200


def _exit_endpoints(descriptor_payload: dict) -> list[str]:
    return [e["endpoint"] for e in descriptor_payload.get("eu_exit_set", [])]


def _desync_strategy(descriptor_payload: dict) -> str | None:
    s = descriptor_payload.get("desync_strategy")
    return s or None


# Current EU-exit endpoint set, as installed by the most recent descriptor
# rewrite. Single writer (descriptor-refresh thread, via _rewrite), single
# reader (periodic-recheck thread). GIL-safe: always replaced wholesale with a
# new list, never mutated in place.
_current_exit_endpoints: list[str] = []


def _apply_desync_on_refresh(*, startup_strategy: str | None, seed, payload: dict) -> list[str]:
    """Manage NFQUEUE desync rules on a descriptor refresh.

    Only boxes provisioned WITH nfqws at startup (a running nfqws process)
    ever install/clear desync rules. A box that started without a strategy
    must NEVER install NFQUEUE rules on a later refresh: there is no nfqws
    process bound to the queue, so the rules would black-hole egress
    (NFQUEUE with no reader -> kernel drop). Updates the current-exit holder."""
    global _current_exit_endpoints
    new_strategy = _desync_strategy(payload)
    new_eps = _exit_endpoints(payload)
    if startup_strategy and seed.nfqws_url:
        # This box has a running nfqws (provisioned at startup).
        if new_strategy:
            with contextlib.suppress(desync.DesyncError):
                desync.install(exit_ips=new_eps, qnum=DESYNC_QNUM)
        else:
            desync.clear(DESYNC_QNUM)
    # else: box started without nfqws -> never touch NFQUEUE rules.
    _current_exit_endpoints = new_eps
    return new_eps

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
    """Run the startup sequence once. Returns (seed, desync_strategy) on
    success; raises _StartupError on any step failure (caller retries — never
    powers off the box). Hardening → seed → mtg binary → configs → iptables →
    optional nfqws/desync."""
    # 1. Apply what the agent can enforce itself (core_pattern, overwritten by
    # apport at boot), then verify all hardening invariants.
    hardening.apply_best_effort()
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

    # 6. Optional desync: fetch nfqws + install NFQUEUE rules for the EU exits.
    strategy = _desync_strategy(descriptor_payload)
    if strategy and s.nfqws_url and s.nfqws_sha256:
        try:
            binary.download_and_verify(
                url=s.nfqws_url, expected_sha256=s.nfqws_sha256, out_path=NFQWS_PATH,
            )
        except binary.BinaryError as e:
            raise _StartupError(f"nfqws binary: {e}") from e
        try:
            desync.install(exit_ips=_exit_endpoints(descriptor_payload), qnum=DESYNC_QNUM)
        except desync.DesyncError as e:
            raise _StartupError(f"desync rules: {e}") from e

    # Seed the current-exit holder for the recheck thread.
    global _current_exit_endpoints
    _current_exit_endpoints = _exit_endpoints(descriptor_payload)

    return s, strategy


def _run_tunnel_check(*, dc_ips, connect_fn=None, log=None, clock=None) -> None:
    """Run the EU tunnel self-check, write health.json, log the verdict.

    Never raises: this runs inside the periodic recheck loop, which must keep
    re-verifying hardening + iptables regardless of the probe outcome."""
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    try:
        v = tunnel_check.check_eu_tunnel(
            dc_ips=dc_ips, connect_fn=connect_fn, clock=clock)
        try:
            tunnel_check.write_health(HEALTH_PATH, v)
        except OSError as e:
            log(f"agent: could not write {HEALTH_PATH}: {e}")
        if v.verdict == "ok":
            log(f"agent: EU tunnel check ok via {v.telegram_dc_tried}")
        else:
            log(f"agent: EU tunnel check FAILED — {v.detail}")
    except Exception as e:  # defensive: the probe must never kill the loop
        log(f"agent: EU tunnel check raised (ignored): {e!r}")


def main() -> int:
    # Startup with bounded retry. On persistent failure, STAY UP (return 2) for
    # diagnosis — do not power off (see STARTUP_MAX_ATTEMPTS comment).
    s = None
    strategy = None
    for attempt in range(1, STARTUP_MAX_ATTEMPTS + 1):
        try:
            res = _startup()
            s, strategy = res
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
        nfqws_cmd=(desync.nfqws_argv(NFQWS_PATH, strategy, qnum=DESYNC_QNUM)
                   if strategy and s.nfqws_url else None),
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

        # Re-apply (or clear) desync rules for the new EU-exit set, and update
        # the holder the periodic-recheck thread reads. Gated on the STARTUP
        # strategy: a box that started without nfqws must never install NFQUEUE
        # rules (no reader -> kernel drop -> egress black-hole).
        _apply_desync_on_refresh(startup_strategy=strategy, seed=s, payload=payload)

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
            # Desync rules: re-verify against the CURRENT exit set. Gate on
            # both the startup-time strategy (whether desync is in play on
            # this box at all) and a non-empty current holder — if a refresh
            # dropped the strategy, _rewrite already cleared the rule and the
            # holder is now empty, so verify_installed on an empty set would
            # trivially pass; an empty gate here just avoids the redundant
            # check (and a spurious terminate if the holder were non-empty but
            # stale during a clear race).
            if strategy and s.nfqws_url:
                eps = list(_current_exit_endpoints)
                if eps and not desync.verify_installed(exit_ips=eps, qnum=DESYNC_QNUM):
                    try:
                        desync.install(exit_ips=eps, qnum=DESYNC_QNUM)
                    except desync.DesyncError as e:
                        _terminate(f"desync: {e}")
                        return
            # K3: end-to-end RU->EU tunnel self-check. Never raises (must not
            # take down the hardening/iptables re-verification loop).
            dc_ips = list(s.telegram_dcs.get("v4", [])) + list(
                s.telegram_dcs.get("v6", []))
            _run_tunnel_check(dc_ips=dc_ips)

    threading.Thread(
        target=_periodic_recheck, daemon=True, name="periodic-recheck",
    ).start()

    # 9. Run supervisor in the main thread.
    sup.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
