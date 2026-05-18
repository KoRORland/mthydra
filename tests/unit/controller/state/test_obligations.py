from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations, prove, set_obligation
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_set_then_prove_updates_timestamp(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_obligation(
        conn,
        obligation_id="t2_dryrun_caseA",
        last_proven_at="2026-05-18T00:00:00Z",
        proven_by="bootstrap",
        next_due_at="2026-06-17T00:00:00Z",
    )
    prove(conn, "t2_dryrun_caseA", proven_by="operator", at="2026-05-19T00:00:00Z", next_due_at="2026-06-18T00:00:00Z", details="dry-run gen-42")
    rows = {r.obligation_id: r for r in list_obligations(conn)}
    assert rows["t2_dryrun_caseA"].last_proven_at == "2026-05-19T00:00:00Z"
    assert rows["t2_dryrun_caseA"].proven_by == "operator"


def test_prove_unknown_obligation_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    try:
        prove(conn, "ghost", "x", "2026-05-19T00:00:00Z", "2026-06-18T00:00:00Z", None)
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")
