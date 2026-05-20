import shutil
import subprocess

import pytest

from mthydra.controller.backup.age_crypt import AgeError, encrypt_file, validate_recipient

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")


@pytest.fixture
def keypair(tmp_path):
    keyfile = tmp_path / "id.key"
    result = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    # `age-keygen` prints the recipient (public key) to stderr in the form `# public key: age1...`
    recipient = ""
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            recipient = line.removeprefix("# public key: ").strip()
            break
    assert recipient.startswith("age1")
    return keyfile, recipient


def test_validate_recipient_accepts_age_pubkey(keypair):
    _, recipient = keypair
    validate_recipient(recipient)


def test_validate_recipient_rejects_garbage():
    with pytest.raises(AgeError):
        validate_recipient("not-an-age-key")


def test_encrypt_then_decrypt_roundtrip(tmp_path, keypair):
    keyfile, recipient = keypair
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"hello world")
    enc = tmp_path / "p.age"
    encrypt_file(plain, recipient=recipient, out=enc)
    assert enc.exists() and enc.stat().st_size > 0
    out = subprocess.run(
        ["age", "-d", "-i", str(keyfile), str(enc)], capture_output=True, check=True
    )
    assert out.stdout == b"hello world"


def test_encrypt_missing_input_raises(tmp_path):
    with pytest.raises(AgeError, match="not found"):
        encrypt_file(tmp_path / "missing", recipient="age1abc", out=tmp_path / "o.age")
