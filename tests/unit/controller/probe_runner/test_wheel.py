from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from mthydra.controller.probe_runner import wheel as wheel_mod
from mthydra.controller.state import schema


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(str(db))
    schema.apply_schema(conn)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni,"
        " state, image_version, created_at) VALUES (?, ?, ?, ?, ?, 'live', ?, ?)",
        ("b-1", "timeweb", "ru-msk-1", "203.0.113.10",
         "www.cloudflare.com", "iv-v2.2.8", now))
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state,"
        " added_at, attested_at, ssh_host, ssh_port, ssh_user, ssh_key_path,"
        " ssh_known_hosts_path) VALUES (?, ?, 'cloud-cis', 'active', ?, ?,"
        " ?, ?, ?, ?, ?)",
        ("ru-msk-1", "ru-msk-1", now, now,
         "203.0.113.5", 22, "probe", "/k", "/kh"))
    conn.commit()
    conn.close()
    return db


def test_wheel_tick_dispatches_probers_and_ingests(monkeypatch, seeded_db):
    # U-D2: the wheel now pre-flights each vantage; stub the reachability
    # check to skip the SSH round-trip in tests that don't care about it.
    monkeypatch.setattr(wheel_mod, "_check_vantage_reachable",
                        lambda v, timeout_s=10: (True, "ok"))
    monkeypatch.setattr(wheel_mod, "ssh_cmd",
                        lambda v, *c, **kw: None)
    monkeypatch.setattr(wheel_mod.probers, "probe_tls_fall_through",
                        lambda fn, ip, sni: ("pass", "tls evidence"))
    monkeypatch.setattr(wheel_mod.probers, "probe_cover_consistency",
                        lambda fn, ip, sni: ("pass", "cover evidence"))
    monkeypatch.setattr(wheel_mod.probers, "probe_surface_scan",
                        lambda fn, ip: ("pass", "surface evidence"))
    recorded = []
    monkeypatch.setattr(wheel_mod, "_record_probe",
        lambda **kw: recorded.append(kw))

    w = wheel_mod.ProbeRunnerWheel(
        db_path=str(seeded_db), interval_seconds=1800, max_concurrent=2,
        mode="offline",
    )
    w.tick()
    assert len(recorded) == 3
    checks = sorted(r["check_type"] for r in recorded)
    assert checks == ["cover_domain_consistency", "surface_scan",
                      "tls_fall_through"]
    assert all(r["box_id"] == "b-1" for r in recorded)
    assert all(r["vantage_id"] == "ru-msk-1" for r in recorded)
    assert all(r["status"] == "pass" for r in recorded)


def test_wheel_tick_skips_vantage_without_ssh(monkeypatch, seeded_db):
    conn = sqlite3.connect(str(seeded_db))
    conn.execute("UPDATE probe_vantages SET ssh_host=NULL WHERE vantage_id=?",
                 ("ru-msk-1",))
    conn.commit()
    conn.close()
    recorded = []
    monkeypatch.setattr(wheel_mod, "_record_probe",
                        lambda **kw: recorded.append(kw))
    w = wheel_mod.ProbeRunnerWheel(db_path=str(seeded_db),
                                   interval_seconds=1800,
                                   max_concurrent=2, mode="offline")
    w.tick()
    assert recorded == []


# ---------------------------------------------------------------------------
# U-D2 — per-vantage pre-flight + failover
# ---------------------------------------------------------------------------


def test_unreachable_vantage_raises_anti_obligation_not_pair_soft_fails(
        monkeypatch, seeded_db):
    """U-D2: when a vantage's SSH transport is broken, the wheel must
    raise ONE probe_vantage_unreachable::<id> anti-obligation and skip
    ALL pair-probes for that vantage — not emit one soft_fail probe row
    per (box × prober) combination."""
    monkeypatch.setattr(wheel_mod, "_check_vantage_reachable",
                        lambda v, timeout_s=10: (False, "ssh-timeout"))
    recorded = []
    monkeypatch.setattr(wheel_mod, "_record_probe",
                        lambda **kw: recorded.append(kw))
    w = wheel_mod.ProbeRunnerWheel(
        db_path=str(seeded_db), interval_seconds=1800,
        max_concurrent=2, mode="offline")
    w.tick()
    assert recorded == []  # no per-pair soft_fail noise
    conn = sqlite3.connect(str(seeded_db))
    row = conn.execute(
        "SELECT obligation_id, details FROM obligation_clocks "
        "WHERE obligation_id=?",
        ("probe_vantage_unreachable::ru-msk-1",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert "ssh-timeout" in row[1]


def test_unreachable_anti_obligation_clears_on_recovery(monkeypatch, seeded_db):
    """U-D2: the next tick where SSH succeeds must clear the anti
    obligation automatically (operator gets self-healing for free)."""
    # First tick: unreachable → flag raised.
    monkeypatch.setattr(wheel_mod, "_check_vantage_reachable",
                        lambda v, timeout_s=10: (False, "boom"))
    w = wheel_mod.ProbeRunnerWheel(
        db_path=str(seeded_db), interval_seconds=1800,
        max_concurrent=2, mode="offline")
    w.tick()
    conn = sqlite3.connect(str(seeded_db))
    assert conn.execute(
        "SELECT 1 FROM obligation_clocks WHERE obligation_id=?",
        ("probe_vantage_unreachable::ru-msk-1",),
    ).fetchone() is not None
    conn.close()
    # Second tick: reachable → flag cleared.
    monkeypatch.setattr(wheel_mod, "_check_vantage_reachable",
                        lambda v, timeout_s=10: (True, "ok"))
    monkeypatch.setattr(wheel_mod, "ssh_cmd", lambda v, *c, **kw: None)
    monkeypatch.setattr(wheel_mod.probers, "probe_tls_fall_through",
                        lambda fn, ip, sni: ("pass", "ok"))
    monkeypatch.setattr(wheel_mod.probers, "probe_cover_consistency",
                        lambda fn, ip, sni: ("pass", "ok"))
    monkeypatch.setattr(wheel_mod.probers, "probe_surface_scan",
                        lambda fn, ip: ("pass", "ok"))
    monkeypatch.setattr(wheel_mod, "_record_probe", lambda **kw: None)
    w.tick()
    conn = sqlite3.connect(str(seeded_db))
    assert conn.execute(
        "SELECT 1 FROM obligation_clocks WHERE obligation_id=?",
        ("probe_vantage_unreachable::ru-msk-1",),
    ).fetchone() is None
    conn.close()


def test_check_vantage_reachable_classifies_ssh_outcomes(monkeypatch):
    """U-D2: SshNotConfigured / timeout / rc!=0 / ok all produce a clean
    one-line reason fit for details_json."""
    class _Fake:
        def __init__(self, rc, stderr=""):
            self.returncode = rc
            self.stderr = stderr
    monkeypatch.setattr(wheel_mod, "ssh_cmd",
                        lambda v, *c, **kw: _Fake(0))
    ok, reason = wheel_mod._check_vantage_reachable({"ssh_host": "x"})
    assert ok is True and reason == "ok"

    monkeypatch.setattr(wheel_mod, "ssh_cmd",
                        lambda v, *c, **kw: _Fake(255, "Permission denied"))
    ok, reason = wheel_mod._check_vantage_reachable({"ssh_host": "x"})
    assert ok is False
    assert "ssh-rc=255" in reason
    assert "Permission denied" in reason
    assert "\n" not in reason

    from mthydra.controller.probe_runner.ssh import SshNotConfigured as _SNC
    def _raises_sshnc(v, *c, **kw):
        raise _SNC("no key")
    monkeypatch.setattr(wheel_mod, "ssh_cmd", _raises_sshnc)
    ok, reason = wheel_mod._check_vantage_reachable({"ssh_host": "x"})
    assert ok is False
    assert "not-configured" in reason
