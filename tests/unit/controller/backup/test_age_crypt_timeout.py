"""M7: encrypt_file bounds the age subprocess with a timeout."""
import subprocess

import pytest

from mthydra.controller.backup import age_crypt
from mthydra.controller.backup.age_crypt import AgeError, encrypt_file

# Canonical valid age recipient (passes bech32 checksum).
GOOD = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"


def test_encrypt_passes_timeout_to_subprocess(tmp_path, monkeypatch):
    src = tmp_path / "p.bin"
    src.write_bytes(b"data")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(age_crypt.subprocess, "run", fake_run)
    encrypt_file(src, recipient=GOOD, out=tmp_path / "o.age")
    assert captured.get("timeout") == 300


def test_encrypt_raises_on_timeout(tmp_path, monkeypatch):
    src = tmp_path / "p.bin"
    src.write_bytes(b"data")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(age_crypt.subprocess, "run", fake_run)
    with pytest.raises(AgeError, match="timed out"):
        encrypt_file(src, recipient=GOOD, out=tmp_path / "o.age")
