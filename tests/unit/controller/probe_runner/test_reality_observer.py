from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime

import pytest

from mthydra.controller.probe_runner.reality_observer import RealityHandshakeObserver
from mthydra.controller.state import eu_exit_set as exit_repo
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.sign import sign_new_descriptor

_NOW = "2026-06-06T00:00:00Z"
_EXIT_FP = "eu-1"
_EXIT_ENDPOINT = "203.0.113.99:443"
_EXIT_SNI = "www.example.com"
_EXIT_PUBKEY = "abcdef0123456789"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)

    # Active probe vantage.
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state,"
        " added_at, attested_at, ssh_host, ssh_port, ssh_user, ssh_key_path,"
        " ssh_known_hosts_path) VALUES (?, ?, 'cloud-cis', 'active', ?, ?,"
        " ?, ?, ?, ?, ?)",
        ("ru-msk-1", "ru-msk-1", _NOW, _NOW,
         "203.0.113.5", 22, "probe", "/k", "/kh"),
    )
    conn.commit()

    # Active descriptor signing key + one EU exit + signed descriptor with
    # tls_fingerprints=(("chrome", 60),).
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, _NOW)
    exit_repo.add_exit(
        conn, _EXIT_FP, _EXIT_ENDPOINT, 100, _NOW,
        cover_sni=_EXIT_SNI, reality_pubkey=_EXIT_PUBKEY,
    )
    sign_new_descriptor(
        conn, now_iso=_NOW, valid_until_iso="2026-06-07T00:00:00Z",
        tls_fingerprints=(("chrome", 60),),
    )
    conn.close()
    return db


def _fake_ssh(scripted, captured=None):
    """Return an ssh_cmd_fn(vantage, *cmd_parts, timeout_s=...) fake.

    `scripted` is either a single (returncode, stdout) pair applied to every
    call, or a callable(cmd_parts) -> (returncode, stdout)."""
    def fn(vantage, *cmd_parts, timeout_s=30):
        if captured is not None:
            captured.append((vantage, cmd_parts))
        if callable(scripted):
            rc, stdout = scripted(cmd_parts)
        else:
            rc, stdout = scripted
        return subprocess.CompletedProcess(
            ("ssh",) + tuple(cmd_parts), rc, stdout, "")
    return fn


def _obligation(db_path, obligation_id):
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT obligation_id, details FROM obligation_clocks WHERE obligation_id=?",
        (obligation_id,),
    ).fetchone()
    conn.close()
    return row


def test_degraded_handshake_raises_anti_obligation(tmp_path, seeded_db):
    captured = []
    fake = _fake_ssh((0, "mthydra-rh result=reset detail=rst\n"), captured)

    obs = RealityHandshakeObserver(
        db_path=seeded_db, ja3_reference_path=None, ssh_cmd_fn=fake,
        clock=_now, mode="offline",
    )
    obs.tick()

    row = _obligation(seeded_db, f"eu_exit_handshake_degraded::{_EXIT_FP}")
    assert row is not None
    details = json.loads(row[1])
    assert details["endpoint"] == _EXIT_ENDPOINT
    assert "reset" in details["verdict"]

    # No staleness obligations: ja3_reference_path is None.
    assert _obligation(seeded_db, "tls_fingerprint_stale::chrome") is None

    # Confirm the fake ssh_cmd_fn was called with the expected shape:
    # (vantage_ssh_dict, "sh", "-c", <cmd containing endpoint host + sni + fingerprint>).
    assert captured, "expected at least one ssh_cmd_fn call"
    vantage, cmd_parts = captured[0]
    assert vantage["vantage_id"] == "ru-msk-1"
    assert cmd_parts[0] == "sh" and cmd_parts[1] == "-c"
    full_cmd = cmd_parts[2]
    assert "203.0.113.99" in full_cmd
    assert _EXIT_SNI in full_cmd
    assert "chrome" in full_cmd


def test_ok_with_matching_ja3_clears_degraded_and_no_stale(tmp_path, seeded_db):
    ref_path = tmp_path / "ja3_ref.json"
    matching_ja3 = "771,4865,0,29,0"
    ref_path.write_text(json.dumps({"chrome": [matching_ja3]}))

    # First tick: degraded, to populate the obligation.
    fake_bad = _fake_ssh((0, "mthydra-rh result=reset detail=rst\n"))
    obs_bad = RealityHandshakeObserver(
        db_path=seeded_db, ja3_reference_path=str(ref_path), ssh_cmd_fn=fake_bad,
        clock=_now, mode="offline",
    )
    obs_bad.tick()
    assert _obligation(seeded_db, f"eu_exit_handshake_degraded::{_EXIT_FP}") is not None

    # Second tick: ok + matching JA3 -> clears degraded, no stale row created.
    fake_ok = _fake_ssh(
        (0, f"mthydra-rh result=ok ja3={matching_ja3} ttfb_ms=20\n"))
    obs_ok = RealityHandshakeObserver(
        db_path=seeded_db, ja3_reference_path=str(ref_path), ssh_cmd_fn=fake_ok,
        clock=_now, mode="offline",
    )
    obs_ok.tick()

    assert _obligation(seeded_db, f"eu_exit_handshake_degraded::{_EXIT_FP}") is None
    assert _obligation(seeded_db, "tls_fingerprint_stale::chrome") is None


def test_ok_with_nonmatching_ja3_creates_stale_obligation(tmp_path, seeded_db):
    ref_path = tmp_path / "ja3_ref.json"
    ref_path.write_text(json.dumps({"chrome": ["771,4865,0,29,0"]}))

    observed_ja3 = "771,9999,0,1,2"
    fake = _fake_ssh((0, f"mthydra-rh result=ok ja3={observed_ja3} ttfb_ms=20\n"))
    obs = RealityHandshakeObserver(
        db_path=seeded_db, ja3_reference_path=str(ref_path), ssh_cmd_fn=fake,
        clock=_now, mode="offline",
    )
    obs.tick()

    assert _obligation(seeded_db, f"eu_exit_handshake_degraded::{_EXIT_FP}") is None
    row = _obligation(seeded_db, "tls_fingerprint_stale::chrome")
    assert row is not None
    details = json.loads(row[1])
    assert details["observed_ja3"] == observed_ja3


def test_no_active_vantage_is_noop(tmp_path, seeded_db):
    conn = sqlite3.connect(str(seeded_db))
    conn.execute("UPDATE probe_vantages SET state='retired'")
    conn.commit()
    conn.close()

    fake = _fake_ssh((0, "mthydra-rh result=reset detail=rst\n"))
    obs = RealityHandshakeObserver(
        db_path=seeded_db, ja3_reference_path=None, ssh_cmd_fn=fake,
        clock=_now, mode="offline",
    )
    obs.tick()
    assert _obligation(seeded_db, f"eu_exit_handshake_degraded::{_EXIT_FP}") is None


def test_no_descriptor_is_noop(tmp_path):
    db = tmp_path / "empty.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state,"
        " added_at, attested_at, ssh_host, ssh_port, ssh_user, ssh_key_path,"
        " ssh_known_hosts_path) VALUES (?, ?, 'cloud-cis', 'active', ?, ?,"
        " ?, ?, ?, ?, ?)",
        ("ru-msk-1", "ru-msk-1", _NOW, _NOW,
         "203.0.113.5", 22, "probe", "/k", "/kh"),
    )
    conn.commit()
    conn.close()

    fake = _fake_ssh((0, "mthydra-rh result=reset detail=rst\n"))
    obs = RealityHandshakeObserver(
        db_path=db, ja3_reference_path=None, ssh_cmd_fn=fake,
        clock=_now, mode="offline",
    )
    obs.tick()  # must not raise
    assert _obligation(db, f"eu_exit_handshake_degraded::{_EXIT_FP}") is None


def test_arm_disarm_offline_noop():
    obs = RealityHandshakeObserver(db_path="/tmp/does-not-matter.sqlite", mode="offline")
    obs.arm()
    assert obs._scheduler is None
    obs.disarm()  # no-op, must not raise
