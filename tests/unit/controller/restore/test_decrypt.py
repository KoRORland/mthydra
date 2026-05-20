"""Tests for age decrypt wrapper (spec A §7.1)."""
import shutil
import subprocess

import pytest

from mthydra.controller.backup.age_crypt import encrypt_file
from mthydra.controller.restore.decrypt import DecryptError, decrypt_blob

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")


@pytest.fixture
def keypair(tmp_path):
    keyfile = tmp_path / "id.key"
    r = subprocess.run(
        ["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True
    )
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in keyfile.read_text().splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, recipient


def test_decrypt_roundtrip(tmp_path, keypair):
    keyfile, recipient = keypair
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"hello world")
    enc = tmp_path / "p.age"
    encrypt_file(plain, recipient, enc)
    out = tmp_path / "r.bin"
    decrypt_blob(enc, identity_path=keyfile, out=out)
    assert out.read_bytes() == b"hello world"


def test_decrypt_wrong_identity_raises(tmp_path, keypair):
    keyfile, recipient = keypair
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"secret")
    enc = tmp_path / "p.age"
    encrypt_file(plain, recipient, enc)
    other = tmp_path / "other.key"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    with pytest.raises(DecryptError):
        decrypt_blob(enc, identity_path=other, out=tmp_path / "r.bin")


def test_decrypt_missing_blob_raises(tmp_path, keypair):
    keyfile, _ = keypair
    with pytest.raises(DecryptError, match="blob not found"):
        decrypt_blob(tmp_path / "nonexistent.age", identity_path=keyfile, out=tmp_path / "r.bin")
