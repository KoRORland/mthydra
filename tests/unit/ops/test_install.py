from __future__ import annotations

from mthydra.ops import install


def test_redacting_log_masks_known_secret_value(tmp_path):
    log_path = tmp_path / "install.log"
    log = install.RedactingLog(
        log_path, secrets={"b2_application_key": "K00super-secret-key"}
    )
    log.write("running with key K00super-secret-key now\n")
    log.close()
    text = log_path.read_text()
    assert "K00super-secret-key" not in text
    assert "***REDACTED:b2_application_key***" in text


def test_redacting_log_masks_age_secret_and_bot_token(tmp_path):
    log = install.RedactingLog(tmp_path / "l.log", secrets={})
    log.write("AGE-SECRET-KEY-1QQPQYU8H4ENHEER9CA2W7XX7XXXXXXXXXXXXXXXXXXXXXXXXXSPLAB\n")
    log.write("token 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789\n")
    log.close()
    text = (tmp_path / "l.log").read_text()
    assert "AGE-SECRET-KEY-1" not in text
    assert "123456789:ABC" not in text
    assert text.count("***REDACTED:") == 2


def test_redacting_log_passes_non_secret_text(tmp_path):
    log = install.RedactingLog(tmp_path / "l.log", secrets={"x": "sekret"})
    log.write("hello world\n")
    log.close()
    assert (tmp_path / "l.log").read_text() == "hello world\n"
