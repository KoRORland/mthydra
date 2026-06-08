"""Tests for VantageSelfCheckSweep — controller self-attests t3."""
from __future__ import annotations

import types

from mthydra.controller.probe_runner.vantage_self_check import (
    VantageSelfCheckSweep,
    default_vantage_check,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.probe_vantages import (
    add_candidate,
    attest_active,
    set_ssh,
)
from mthydra.controller.state.schema import apply_schema

NOW = "2026-06-08T00:00:00Z"


def _db(tmp_path):
    p = tmp_path / "state.sqlite"
    c = connect(p)
    apply_schema(c)
    c.close()
    return p


def _active_vantage(db, vantage_id="v1"):
    conn = connect(db)
    add_candidate(conn, vantage_id=vantage_id, label=vantage_id,
                  source_kind="vps", at=NOW)
    attest_active(conn, vantage_id, at=NOW)
    set_ssh(conn, vantage_id, host="1.2.3.4", port=22, user="probe",
            key_path="/k", known_hosts_path="/kh")
    conn.commit()
    conn.close()


def _t3(db):
    conn = connect(db)
    try:
        return {o.obligation_id: o for o in list_obligations(conn)}.get(
            "t3_vantage_revalidation")
    finally:
        conn.close()


def test_no_active_vantage_is_noop(tmp_path):
    db = _db(tmp_path)
    sweep = VantageSelfCheckSweep(
        db_path=db, sweep_interval_seconds=1800, cover_sni_ref=None,
        mode="offline", clock=lambda: NOW,
        check_fn=lambda v, ref: (True, "ok"),
    )
    assert sweep.run_once() == {"checked": 0, "passed": 0}
    assert _t3(db) is None  # t3 untouched — left to the operator


def test_passing_vantage_self_proves_t3(tmp_path):
    db = _db(tmp_path)
    _active_vantage(db)
    sweep = VantageSelfCheckSweep(
        db_path=db, sweep_interval_seconds=1800, cover_sni_ref="cover.example",
        mode="offline", clock=lambda: NOW,
        check_fn=lambda v, ref: (True, "tls_ok"),
    )
    assert sweep.run_once() == {"checked": 1, "passed": 1}
    ob = _t3(db)
    assert ob is not None
    assert ob.proven_by == "vantage_self_check"


def test_all_failing_vantages_do_not_prove_t3(tmp_path):
    db = _db(tmp_path)
    _active_vantage(db)
    sweep = VantageSelfCheckSweep(
        db_path=db, sweep_interval_seconds=1800, cover_sni_ref="cover.example",
        mode="offline", clock=lambda: NOW,
        check_fn=lambda v, ref: (False, "tls_fail"),
    )
    assert sweep.run_once() == {"checked": 1, "passed": 0}
    assert _t3(db) is None  # not proven -> t3 goes overdue -> operator alerted


# --- default_vantage_check (the real probe logic, ssh injected) -------------

def _fake_ssh(returncode, stdout):
    def _run(vantage, *cmd_parts, timeout_s=30):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return _run


def test_default_check_tls_verify_ok_passes():
    ok, reason = default_vantage_check(
        {"ssh_host": "h", "ssh_user": "u", "ssh_key_path": "/k"},
        "cover.example",
        ssh_cmd_fn=_fake_ssh(0, "... Verify return code: 0 (ok) ..."),
    )
    assert ok is True and reason == "tls_ok"


def test_default_check_tls_verify_fail_fails():
    ok, reason = default_vantage_check(
        {"ssh_host": "h", "ssh_user": "u", "ssh_key_path": "/k"},
        "cover.example",
        ssh_cmd_fn=_fake_ssh(0, "... Verify return code: 21 (unable to verify) ..."),
    )
    assert ok is False and reason == "tls_fail"


def test_default_check_no_ref_falls_back_to_ssh_liveness():
    ok, reason = default_vantage_check(
        {"ssh_host": "h", "ssh_user": "u", "ssh_key_path": "/k"},
        None,
        ssh_cmd_fn=_fake_ssh(0, "OK\n"),
    )
    assert ok is True and reason == "ssh_ok"


def test_default_check_swallows_ssh_errors():
    def _boom(vantage, *cmd_parts, timeout_s=30):
        raise TimeoutError("ssh timed out")
    ok, reason = default_vantage_check(
        {"ssh_host": "h", "ssh_user": "u", "ssh_key_path": "/k"},
        "cover.example", ssh_cmd_fn=_boom,
    )
    assert ok is False and reason.startswith("error:")
