import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "state.sqlite"
