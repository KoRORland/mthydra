"""Task 3 — _pick_fingerprint deterministic per-box uTLS selection."""
import pytest
from mthydra.ru_agent.config_gen import _pick_fingerprint, ConfigError


def test_none_list_falls_back_to_chrome():
    assert _pick_fingerprint("box-1", None) == "chrome"
    assert _pick_fingerprint("box-1", []) == "chrome"


def test_pick_is_deterministic_per_box():
    wl = [{"fp": "chrome", "weight": 60}, {"fp": "firefox", "weight": 40}]
    assert _pick_fingerprint("box-abc", wl) == _pick_fingerprint("box-abc", wl)
    # Pinned to the observed hash-scheme output so a regression is detectable.
    assert _pick_fingerprint("box-abc", wl) == "chrome"


def test_pick_varies_across_boxes():
    wl = [{"fp": "chrome", "weight": 1}, {"fp": "firefox", "weight": 1}]
    assert {_pick_fingerprint(f"box-{i}", wl) for i in range(50)} == {"chrome", "firefox"}


def test_unknown_fingerprint_raises():
    with pytest.raises(ConfigError):
        _pick_fingerprint("box-1", [{"fp": "nessuno", "weight": 1}])


def test_weight_respected():
    wl = [{"fp": "chrome", "weight": 0}, {"fp": "firefox", "weight": 5}]
    assert {_pick_fingerprint(f"b{i}", wl) for i in range(20)} == {"firefox"}


def test_malformed_entry_raises():
    with pytest.raises(ConfigError):
        _pick_fingerprint("box-1", [{"fp": "chrome"}])            # missing weight
    with pytest.raises(ConfigError):
        _pick_fingerprint("box-1", [{"fp": "chrome", "weight": "heavy"}])  # non-int
