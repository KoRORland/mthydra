import shutil
import sys
from pathlib import Path

import pytest

# The backup monitor ships as a separate wheel (mthydra-backup-monitor/) and is
# not installed into this repo's venv. Put its src on sys.path so integration
# tests that import it (e.g. test_gap_monitor) can be collected without a
# separate `pip install -e mthydra-backup-monitor/`.
_MONITOR_SRC = Path(__file__).resolve().parent.parent / "mthydra-backup-monitor" / "src"
if _MONITOR_SRC.is_dir() and str(_MONITOR_SRC) not in sys.path:
    sys.path.insert(0, str(_MONITOR_SRC))


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
