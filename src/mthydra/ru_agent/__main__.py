"""mthydra RU agent — long-lived supervisor.

Reads /run/mthydra/seed.json, verifies it, downloads mtg, writes mtg and
sing-box configs, installs iptables rules, launches both children, runs
the descriptor refresh loop, terminates the box on persistent failure.
"""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path

from mthydra.ru_agent import (
    binary,
    config_gen,
    descriptor_refresh,
    hardening,
    iptables,
    seed as seed_mod,
    shutdown as shutdown_mod,
    supervisor,
)


SEED_PATH = "/run/mthydra/seed.json"
MTG_PATH = "/run/mthydra/mtg"
MTG_CONFIG_PATH = "/run/mthydra/mtg.toml"
SING_BOX_CONFIG_PATH = "/run/mthydra/sing-box.json"
TPROXY_PORT = 12345


def _terminate(reason: str) -> None:
    shutdown_mod.terminate_box(reason)


def main() -> int:
    # 1. Hardening verification.
    try:
        hardening.verify_all()
    except hardening.HardeningError as e:
        print(f"agent: hardening failed: {e}", file=sys.stderr)
        _terminate(f"hardening: {e}")
        return 2

    # 2. Load + verify seed.
    try:
        s = seed_mod.load(SEED_PATH)
        seed_mod.verify_credential(s)
    except seed_mod.SeedError as e:
        print(f"agent: seed invalid: {e}", file=sys.stderr)
        _terminate(f"seed: {e}")
        return 2

    # 3. Download + verify mtg binary.
    try:
        binary.download_and_verify(
            url=s.image["url"],
            expected_sha256=s.image["sha256"],
            out_path=MTG_PATH,
        )
    except binary.BinaryError as e:
        print(f"agent: binary download failed: {e}", file=sys.stderr)
        _terminate(f"binary: {e}")
        return 2

    # 4. Parse initial descriptor and render configs.
    import base64, json, struct
    blob = s.initial_descriptor
    n = struct.unpack(">H", blob[:2])[0]
    descriptor_payload = json.loads(blob[2:2 + n])

    mtg_toml = config_gen.render_mtg_config(s, sing_box_socks_port=TPROXY_PORT)
    Path(MTG_CONFIG_PATH).write_bytes(mtg_toml)
    sing_box_json = config_gen.render_sing_box_config(
        s, descriptor_payload, tproxy_port=TPROXY_PORT,
    )
    Path(SING_BOX_CONFIG_PATH).write_bytes(sing_box_json)

    # 5. Install iptables rules.
    try:
        iptables.install(
            dc_cidrs_v4=s.telegram_dcs.get("v4", []),
            dc_cidrs_v6=s.telegram_dcs.get("v6", []),
            tproxy_port=TPROXY_PORT,
        )
    except iptables.IptablesError as e:
        print(f"agent: iptables install failed: {e}", file=sys.stderr)
        _terminate(f"iptables: {e}")
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
        import struct
        n = struct.unpack(">H", blob[:2])[0]
        payload = json.loads(blob[2:2 + n])
        new_json = config_gen.render_sing_box_config(
            s, payload, tproxy_port=TPROXY_PORT,
        )
        Path(SING_BOX_CONFIG_PATH).write_bytes(new_json)
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
