from __future__ import annotations

import textwrap

import pytest

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


def test_missing_required_field_errors_when_non_interactive(tmp_path):
    ini = _write_ini(tmp_path, _FULL_INI.replace("hostname = eu1.example.com", "hostname ="))
    with pytest.raises(install.ConfigError, match="hostname"):
        install.load_config(ini, role="active", promote=False,
                            interactive=False, env={})


def test_interactive_prompt_fills_missing_field(tmp_path, monkeypatch):
    ini = _write_ini(tmp_path, _FULL_INI.replace("hostname = eu1.example.com", "hostname ="))
    monkeypatch.setattr("builtins.input", lambda prompt="": "typed.example.com")
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=True, env={})
    assert cfg.hostname == "typed.example.com"


def test_secret_prompt_uses_getpass(tmp_path, monkeypatch):
    ini = _write_ini(tmp_path, _FULL_INI.replace("application_key = B2SECRET", "application_key ="))
    called = {}
    def fake_getpass(prompt=""):
        called["yes"] = True
        return "TYPED_SECRET"
    monkeypatch.setattr(install.getpass, "getpass", fake_getpass)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=True, env={})
    assert cfg.b2_application_key == "TYPED_SECRET"
    assert called.get("yes") is True


def test_refuses_age_secret_key(tmp_path):
    bad = _FULL_INI.replace(
        "recipient = age1qqp0000000000000000000000000000000000000000000000000q",
        "recipient = AGE-SECRET-KEY-1QQPQYU8H4ENHEER9CA2W7XXXXXXXXXXXXXXXXXXXXXXXXXSPLAB",
    )
    ini = _write_ini(tmp_path, bad)
    with pytest.raises(install.ConfigError, match="secret key"):
        install.load_config(ini, role="active", promote=False,
                            interactive=False, env={})


def test_passive_standby_allows_missing_sinks(tmp_path):
    minimal = """\
        [install]
        git_url = https://example/mthydra.git
        [node]
        hostname = standby.example.com
        [age]
        recipient = age1qqp0000000000000000000000000000000000000000000000000q
        [backup]
        endpoint = https://s3.example.com
        bucket = mthydra-prod
        key_id = 0012abc
        application_key = B2SECRET
        """
    ini = _write_ini(tmp_path, minimal)
    cfg = install.load_config(ini, role="standby", promote=False,
                              interactive=False, env={})
    assert cfg.obs_tg_bot_token == ""  # not required, not prompted


def test_promote_standby_requires_sinks(tmp_path):
    minimal = """\
        [install]
        git_url = https://example/mthydra.git
        [node]
        hostname = standby.example.com
        [age]
        recipient = age1qqp0000000000000000000000000000000000000000000000000q
        [backup]
        endpoint = https://s3.example.com
        bucket = mthydra-prod
        key_id = 0012abc
        application_key = B2SECRET
        """
    ini = _write_ini(tmp_path, minimal)
    with pytest.raises(install.ConfigError, match="obs_tg_bot_token"):
        install.load_config(ini, role="standby", promote=True,
                            interactive=False, env={})


def _ctx(tmp_path, dry_run=False):
    ini = _write_ini(tmp_path, _FULL_INI)
    cfg = install.load_config(ini, role="active", promote=False,
                              interactive=False, env={})
    log = install.RedactingLog(tmp_path / "i.log", cfg.secret_values())
    return install.Ctx(config=cfg, log=log, dry_run=dry_run, quiet=True)


def test_runner_skips_satisfied_phases(tmp_path):
    ctx = _ctx(tmp_path)
    ran = []
    phases = [
        install.Phase("a", lambda c: True, lambda c: ran.append("a")),
        install.Phase("b", lambda c: False, lambda c: ran.append("b")),
    ]
    rc = install.Runner(phases, ctx).execute()
    assert rc == 0
    assert ran == ["b"]  # 'a' skipped


def test_runner_dry_run_executes_nothing(tmp_path):
    ctx = _ctx(tmp_path, dry_run=True)
    ran = []
    phases = [install.Phase("b", lambda c: False, lambda c: ran.append("b"))]
    rc = install.Runner(phases, ctx).execute()
    assert rc == 0
    assert ran == []


def test_runner_aborts_on_phase_exception(tmp_path):
    ctx = _ctx(tmp_path)
    ran = []
    def boom(c):
        raise RuntimeError("kaboom")
    phases = [
        install.Phase("a", lambda c: False, boom),
        install.Phase("b", lambda c: False, lambda c: ran.append("b")),
    ]
    rc = install.Runner(phases, ctx).execute()
    assert rc == 1
    assert ran == []  # pipeline stopped before 'b'
