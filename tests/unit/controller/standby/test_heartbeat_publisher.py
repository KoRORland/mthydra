"""Spec F — StandbyHeartbeatPublisher."""
import json
from unittest.mock import MagicMock

from mthydra.controller.standby.heartbeat import StandbyHeartbeatPublisher


def test_publisher_run_once_uploads_well_formed_json():
    dest = MagicMock()
    pub = StandbyHeartbeatPublisher(
        node_id="eu-standby-de-1",
        b2_destination=dest,
        interval_seconds=60,
        mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    pub.run_once()
    dest.put_heartbeat.assert_called_once()
    call_kwargs = dest.put_heartbeat.call_args.kwargs
    assert call_kwargs["node_id"] == "eu-standby-de-1"
    payload = json.loads(call_kwargs["payload"])
    assert payload["schema"] == "mthydra.standby_heartbeat.v1"
    assert payload["node_id"] == "eu-standby-de-1"
    assert payload["ts"] == "2026-05-20T01:00:00Z"
    assert payload["schema_version"] == 4
    assert isinstance(payload["controller_version"], str)


def test_publisher_offline_mode_arm_is_noop():
    dest = MagicMock()
    pub = StandbyHeartbeatPublisher(
        node_id="x", b2_destination=dest,
        interval_seconds=60, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    pub.arm()
    pub.disarm()
    dest.put_heartbeat.assert_not_called()


def test_publisher_run_once_callable_even_in_offline_mode():
    dest = MagicMock()
    pub = StandbyHeartbeatPublisher(
        node_id="x", b2_destination=dest,
        interval_seconds=60, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    pub.run_once()
    dest.put_heartbeat.assert_called_once()
