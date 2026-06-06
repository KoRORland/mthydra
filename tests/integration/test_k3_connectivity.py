"""K3 integration: box-side broken-tunnel verdict + controller-side unseen alert."""
from __future__ import annotations

from mthydra.controller.data_exit.exit_observer import EuExitObserver
from mthydra.controller.observability.snapshot import collect_snapshot
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.ru_agent import tunnel_check as tc


def test_broken_tunnel_box_verdict_is_fail():
    """Box-side: a broken upstream (sing-box closes -> EOF) yields FAIL."""
    class Dead:
        def sendall(self, d):
            pass

        def recv(self, n):
            return b""  # EOF

        def close(self):
            pass

    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda *a, **k: Dead(),
        clock=lambda: "2026-06-06T10:00:00Z")
    assert v.verdict == "fail"


def test_unseen_live_box_surfaces_alert_end_to_end(tmp_path):
    """Controller-side: a live box never seen at the exit surfaces the
    box_eu_tunnel_unseen anti-obligation through the snapshot."""
    db = tmp_path / "s.sqlite"
    c = connect(db)
    apply_schema(c)
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, "
        "image_version, created_at, reality_uuid) VALUES "
        "('box-1', 'prov', 'reg', 'sni-1', 'live', 'img1', "
        "'2026-06-01T00:00:00Z', 'uuid-1')")
    c.commit()
    c.close()

    EuExitObserver(
        db_path=db, clash_api_url="http://127.0.0.1:9090",
        poll_fn=lambda url, timeout=5.0: set(),   # exit sees nobody
        clock=lambda: "2026-06-06T10:00:00Z",
        unseen_threshold_seconds=900, mode="offline").tick()

    c = connect(db)
    snap = collect_snapshot(c, now="2026-06-06T10:01:00Z")
    kinds = {a.kind for a in snap.anti_obligations}
    assert "box_eu_tunnel_unseen" in kinds
