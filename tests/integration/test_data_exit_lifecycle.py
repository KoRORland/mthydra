"""Spec E — EU data-exit lifecycle test.

Provision 3 RU boxes (via provision-seed CLI flow), drive the wheel tick,
assert sing-box config contains all 3 UUIDs, revoke one credential,
tick again, assert it's removed, terminate another box, tick, verify
removal.
"""
import json
from pathlib import Path


def test_data_exit_lifecycle_full(tmp_path, age_recipient, monkeypatch):
    from mthydra.controller.cli import run
    from mthydra.controller.config import load_config
    from mthydra.controller.data_exit.wheel import DataExitWheel
    from mthydra.controller.state.credentials import revoke_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import (
        add_eu_node, set_data_exit_identity,
    )

    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_TOML_WITH_DATA_EXIT.format(
        config_path=str(tmp_path / "sb.json"),
        reality_key_path=str(tmp_path / "r.key"),
    ))
    (tmp_path / "r.key").write_text("PRIVKEY")

    # 1. init + authority-migrate-placeholder (spec-G CLI).
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db),
         "--config", str(cfg_path)])

    # Stub S3Destination.presigned_image_url since we don't have real B2.
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-23T01:00:00Z",
        ),
    )

    # Set up image + 3 cover domains + signed descriptor.
    from mthydra.controller.state.cover_pool import (
        add_candidate, attest_verified,
    )
    from mthydra.controller.state.ru_images import insert_candidate, promote
    conn = connect(db)
    insert_candidate(conn, image_version="abc", upstream_release="v2.1.7",
                     upstream_repo="9seconds/mtg",
                     binary_url="images/abc/mtg",
                     manifest_url="images/abc/manifest.json",
                     binary_sha256="abc", binary_size_bytes=10485760,
                     built_at="2026-05-23T00:00:00Z")
    promote(conn, "abc", at="2026-05-23T00:01:00Z", evidence="smoke")
    for i in range(3):
        add_candidate(conn, f"cover{i}.example",
                      added_at="2026-05-23T00:00:00Z")
        attest_verified(conn, f"cover{i}.example", from_vantage="ru-vps",
                        at="2026-05-23T00:00:00Z")
    conn.close()
    run(["descriptor-sign-now", "--db-path", str(db),
         "--config", str(cfg_path)])

    # Provision 3 boxes via the CLI.
    box_ids = []
    for i in range(3):
        run([
            "provision-seed", "--format", "json",
            "--provider", "hetzner", "--region", f"r{i}",
            "--db-path", str(db), "--config", str(cfg_path),
            "--descriptor-refresh-url", "https://b2/desc",
            "--agent-source-url", "https://b2/agent.tar.gz",
            "--agent-source-sha256", "deadbeef" * 8,
        ])
        conn = connect(db)
        # Pick the freshly-provisioned box (only one is in 'provisioning'
        # state at a time; created_at is second-granularity so it can tie).
        bid = conn.execute(
            "SELECT box_id FROM ru_boxes WHERE state='provisioning' LIMIT 1"
        ).fetchone()[0]
        box_ids.append(bid)
        conn.execute(
            "UPDATE ru_boxes SET state='live', public_ip=? WHERE box_id=?",
            (f"203.0.113.{i+1}", bid),
        )
        conn.commit()
        conn.close()

    # 2. Add an eu_node, set identity.
    conn = connect(db)
    add_eu_node(conn, node_id="eu1", hostname="eu1.example", provider="p",
                region="r", role="active", added_at="2026-05-23T00:00:00Z")
    conn.execute(
        "UPDATE eu_nodes SET public_ip='203.0.113.99' WHERE node_id='eu1'"
    )
    set_data_exit_identity(conn, "eu1", cover_sni="eucover.example",
                           reality_pubkey="EUPUB")
    conn.commit()
    conn.close()

    # 3. Build the wheel + run tick. SIGHUP is a no-op for the test.
    cfg = load_config(cfg_path)
    wheel = DataExitWheel(
        db_path=db, cfg=cfg.data_exit, node_id="eu1",
        sighup_fn=lambda u: None,
        now_fn=lambda: "2026-05-23T00:10:00Z",
        mode="offline",
    )
    wheel.tick()

    payload = json.loads(Path(tmp_path / "sb.json").read_text())
    users = payload["inbounds"][0]["users"]
    assert len(users) == 3
    assert {u["name"] for u in users} == set(box_ids)

    # 4. Revoke box_ids[0]'s credential -> next tick removes it.
    conn = connect(db)
    cred_id = conn.execute(
        "SELECT cred_id FROM onward_credentials WHERE box_id=?",
        (box_ids[0],),
    ).fetchone()[0]
    revoke_credential(conn, cred_id, at="2026-05-23T00:11:00Z")
    conn.close()
    wheel.tick()
    payload = json.loads(Path(tmp_path / "sb.json").read_text())
    users = payload["inbounds"][0]["users"]
    assert len(users) == 2

    # 5. Terminate box_ids[1] -> next tick removes it.
    run(["ru-box-terminate", box_ids[1], "--reason", "test",
         "--db-path", str(db)])
    wheel.tick()
    payload = json.loads(Path(tmp_path / "sb.json").read_text())
    users = payload["inbounds"][0]["users"]
    assert len(users) == 1
    assert users[0]["name"] == box_ids[2]

    # 6. eu_exit_set row exists after first tick.
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM eu_exit_set WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert n == 1
    conn.close()


_TOML_WITH_DATA_EXIT = """\
[node]
role = "active"
hostname = "h"
[backup]
floor_interval_hours = 24
on_change_debounce_seconds = 30
endpoint = "https://example"
bucket = "b"
access_key_id = "k"
[backup.retention]
keep_daily = 30
keep_monthly = 12
object_lock_days = 365
[gap_monitor]
poll_interval_minutes = 30
alarm_threshold_hours = 48
recipient_email = "op@example.org"
[descriptor]
rotation_interval_hours = 1
validity_window_hours = 24
[obligations]
[obligations.timers_hours]
[cover_pool]
rotation_ttl_days = 14
reverify_after_days = 30
freeze_threshold = 2
reverify_sweep_interval = "1h"
rotation_sweep_interval = "1h"
replenishment_interval_days = 90
[data_exit]
listen_port = 443
sing_box_socket = "/run/sb.sock"
config_path = "{config_path}"
reality_key_path = "{reality_key_path}"
[data_exit.telegram_dcs]
v4 = ["149.154.160.0/20"]
v6 = []
[data_exit.cover_sni]
default = "fallback.example"
"""
