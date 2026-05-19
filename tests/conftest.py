import shutil

import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "state.sqlite"


@pytest.fixture
def age_recipient(tmp_path):
    """Real age X25519 public-key recipient; skips when age-keygen is unavailable."""
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    import subprocess
    keyfile = tmp_path / "identity"
    r = subprocess.run(
        ["age-keygen", "-o", str(keyfile)],
        capture_output=True, text=True, check=True,
    )
    return next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
