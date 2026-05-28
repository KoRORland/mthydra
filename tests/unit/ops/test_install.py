from __future__ import annotations

import textwrap

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


def _write_ini(tmp_path, body: str):
    p = tmp_path / "install.ini"
    p.write_text(textwrap.dedent(body))
    return p


_FULL_INI = """\
    [install]
    git_url = https://example/mthydra.git
    git_ref = v1.0.0
    [node]
    hostname = eu1.example.com
    [age]
    recipient = age1qqp0000000000000000000000000000000000000000000000000q
    [backup]
    endpoint = https://s3.example.com
    bucket = mthydra-prod
    key_id = 0012abc
    application_key = B2SECRET
    [observability.telegram]
    bot_token = 111:AAA
    chat_id = 9999
    [observability.email]
    smtp_host = smtp.example.com
    smtp_port = 587
    from_addr = alerts@example.com
    to_addr = op@example.com
    username = alerts@example.com
    password = OBSPASS
    [distribution.telegram]
    bot_token = 222:BBB
    [distribution.email]
    smtp_host = smtp.example.com
    smtp_port = 587
    from_addr = dist@example.com
    username = dist@example.com
    password = DISTPASS
    """


def test_load_config_parses_full_ini(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False, env={})
    assert cfg.hostname == "eu1.example.com"
    assert cfg.b2_application_key == "B2SECRET"
    assert cfg.obs_smtp_port == 587
    assert cfg.git_ref == "v1.0.0"


def test_b2_application_key_env_wins_over_ini(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False,
                              env={"B2_APPLICATION_KEY": "FROM_ENV"})
    assert cfg.b2_application_key == "FROM_ENV"


def test_secret_values_returns_only_nonempty_secrets(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False, env={})
    sv = cfg.secret_values()
    assert sv["b2_application_key"] == "B2SECRET"
    assert sv["obs_smtp_pass"] == "OBSPASS"
    assert set(sv) == install.SECRET_FIELDS
