from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.credentials import active_for_box, issue_credential, revoke_credential
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    return conn


def test_issue_returns_unique_cred(tmp_db_path):
    conn = _conn(tmp_db_path)
    c1 = issue_credential(conn, box_id="box-1", credential=b"S1", issued_at="2026-05-18T00:01:00Z", authority_generation=1)
    c2 = issue_credential(conn, box_id="box-1", credential=b"S2", issued_at="2026-05-18T00:02:00Z", authority_generation=1)
    assert c1 != c2


def test_active_for_box_excludes_revoked(tmp_db_path):
    conn = _conn(tmp_db_path)
    cid = issue_credential(conn, "box-1", b"S1", "2026-05-18T00:01:00Z", 1)
    revoke_credential(conn, cid, at="2026-05-18T00:05:00Z")
    assert active_for_box(conn, "box-1") == []
