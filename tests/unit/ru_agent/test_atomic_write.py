"""M1: ru_agent config writes are atomic (no torn/partial files for sing-box)."""
import os

from mthydra.ru_agent.__main__ import _atomic_write_bytes


def test_atomic_write_creates_file_with_content(tmp_path):
    target = tmp_path / "sing-box.json"
    _atomic_write_bytes(str(target), b'{"a":1}')
    assert target.read_bytes() == b'{"a":1}'


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "sing-box.json"
    target.write_bytes(b"old-and-longer-content")
    _atomic_write_bytes(str(target), b"new")
    assert target.read_bytes() == b"new"


def test_atomic_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "cfg.json"
    _atomic_write_bytes(str(target), b"x")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "cfg.json"]
    assert leftovers == []


def test_atomic_write_uses_replace_not_truncate(tmp_path, monkeypatch):
    """The new bytes must arrive via os.replace, so a reader never sees a
    truncated file. Assert os.replace is the mechanism."""
    target = tmp_path / "cfg.json"
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["called"] = (str(src), str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    _atomic_write_bytes(str(target), b"data")
    assert seen["called"][1] == str(target)
    assert target.read_bytes() == b"data"
