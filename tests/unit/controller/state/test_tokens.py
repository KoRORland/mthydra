import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.tokens import (
    get_provider_credential,
    get_publishing_token,
    set_provider_credential,
    set_publishing_token,
)


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_set_then_get_publishing_token(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_publishing_token(conn, kind="telegram_bot", value="bot:xyz", at="2026-05-18T00:00:00Z")
    assert get_publishing_token(conn, "telegram_bot") == "bot:xyz"


def test_replace_publishing_token(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_publishing_token(conn, kind="telegram_bot", value="bot:old", at="2026-05-18T00:00:00Z")
    set_publishing_token(conn, kind="telegram_bot", value="bot:new", at="2026-05-19T00:00:00Z")
    assert get_publishing_token(conn, "telegram_bot") == "bot:new"


def test_get_missing_provider_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(LookupError):
        get_provider_credential(conn, "aws")


def test_set_then_get_provider_credential(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_provider_credential(conn, provider="aws", credential="AKID:SECRET", at="2026-05-18T00:00:00Z")
    assert get_provider_credential(conn, "aws") == "AKID:SECRET"
