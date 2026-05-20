import shutil

import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "state.sqlite"


@pytest.fixture
def age_recipient(tmp_path):
    """Real age X25519 public-key recipient; skips when age-keygen is unavailable.

    Parses the public-key line out of the generated keyfile (which is a portable
    format across age implementations); the stderr format differs between
    distributions (Fedora's age writes 'Public key:' without the '# ' prefix
    used by upstream FiloSottile/age).
    """
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    import subprocess
    keyfile = tmp_path / "identity"
    subprocess.run(
        ["age-keygen", "-o", str(keyfile)],
        capture_output=True, text=True, check=True,
    )
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return line.removeprefix("# public key: ").strip()
    raise RuntimeError(f"age-keygen produced no '# public key:' line in {keyfile}")
