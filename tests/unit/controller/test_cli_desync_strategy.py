"""Tests for the `desync-strategy-{show,stage,promote,mark-proven}` CLI
subcommands (V2 Task 8 — spec V V-D6 / invariant #36 canary gate)."""
from __future__ import annotations

from mthydra.controller import cli
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _db(tmp_path):
    path = tmp_path / "state.sqlite"
    conn = connect(path)
    apply_schema(conn)
    conn.close()
    return str(path)


def test_stage_show_promote_flow(tmp_path, capsys):
    db = _db(tmp_path)

    assert cli.run(["desync-strategy-stage", "--db-path", db,
                    "--strategy", "--dpi-desync=fake"]) == 0
    capsys.readouterr()

    assert cli.run(["desync-strategy-show", "--db-path", db]) == 0
    out = capsys.readouterr().out
    assert "--dpi-desync=fake" in out
    assert "canary" in out.lower()

    rc = cli.run(["desync-strategy-promote", "--db-path", db])
    assert rc != 0
    assert "canary" in capsys.readouterr().err.lower()

    assert cli.run(["desync-strategy-mark-proven", "--db-path", db,
                    "--strategy", "--dpi-desync=fake"]) == 0
    capsys.readouterr()

    assert cli.run(["desync-strategy-promote", "--db-path", db]) == 0
    out = capsys.readouterr().out
    assert "--dpi-desync=fake" in out


def test_promote_with_no_staged_strategy_fails(tmp_path, capsys):
    db = _db(tmp_path)
    rc = cli.run(["desync-strategy-promote", "--db-path", db])
    assert rc != 0
    assert "no staged strategy" in capsys.readouterr().err.lower()


def test_show_reports_not_canary_proven_before_marking(tmp_path, capsys):
    db = _db(tmp_path)
    assert cli.run(["desync-strategy-stage", "--db-path", db,
                    "--strategy", "--dpi-desync=fake"]) == 0
    capsys.readouterr()
    assert cli.run(["desync-strategy-show", "--db-path", db]) == 0
    out = capsys.readouterr().out
    assert "not canary-proven" in out.lower() or "canary-proven: no" in out.lower()
