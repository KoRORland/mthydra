from __future__ import annotations

import json

from mthydra.controller.distribution.sinks import TelegramDistributionSink


def test_get_me_returns_username():
    def fake_get(url, params):
        assert "getMe" in url
        return 200, json.dumps({"ok": True, "result": {"username": "myfam_bot"}})
    s = TelegramDistributionSink(bot_token="t", http_get=fake_get)
    assert s.get_me() == "myfam_bot"


def test_get_updates_passes_offset_and_parses():
    seen = {}
    def fake_get(url, params):
        seen.update(params)
        return 200, json.dumps({"ok": True, "result": [
            {"update_id": 41, "message": {"chat": {"id": 99}, "text": "/start AB"}},
        ]})
    s = TelegramDistributionSink(bot_token="t", http_get=fake_get)
    updates = s.get_updates(offset=41)
    assert seen["offset"] == 41
    assert updates == [{"update_id": 41, "chat_id": "99", "text": "/start AB"}]


def test_get_updates_skips_non_message_updates():
    def fake_get(url, params):
        return 200, json.dumps({"ok": True, "result": [
            {"update_id": 7, "edited_message": {"chat": {"id": 1}, "text": "x"}},
        ]})
    s = TelegramDistributionSink(bot_token="t", http_get=fake_get)
    assert s.get_updates(offset=0) == []
