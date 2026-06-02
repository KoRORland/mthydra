from __future__ import annotations

import argparse
import json
import ssl
import subprocess
from pathlib import Path

import pytest

from mthydra.ops import ru_bringup


def test_wait_for_reachable_returns_true_on_handshake(monkeypatch):
    # Mock socket.create_connection → fake socket; mock ssl context → handshake OK.
    class _FakeTLS:
        def do_handshake(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    class _FakeCtx:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        def wrap_socket(self, sock, server_hostname=None): return _FakeTLS()
    class _FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(ru_bringup.socket, "create_connection",
                        lambda addr, timeout=None: _FakeSock())
    monkeypatch.setattr(ru_bringup.ssl, "create_default_context", lambda: _FakeCtx())
    assert ru_bringup.wait_for_reachable("1.2.3.4", 443, "sni.example",
                                         timeout_s=1, poll_s=0) is True


def test_wait_for_reachable_returns_false_on_timeout(monkeypatch):
    def boom(addr, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr(ru_bringup.socket, "create_connection", boom)
    # Advance fake clock past the deadline immediately.
    times = iter([0.0, 0.5, 2.0])  # start, after first attempt, past deadline=1.0
    monkeypatch.setattr(ru_bringup.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ru_bringup.time, "sleep", lambda s: None)
    progress = []
    assert ru_bringup.wait_for_reachable("1.2.3.4", 443, "sni",
                                         timeout_s=1, poll_s=0,
                                         on_progress=progress.append) is False
    assert progress  # called at least once with the error


def _fake_run_factory(stdout_map=None, stderr_map=None, default_rc=0):
    """stdout_map / stderr_map: dict keyed by the first controller subcommand."""
    stdout_map = stdout_map or {}
    stderr_map = stderr_map or {}
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        sub = args[0] if args else ""
        return subprocess.CompletedProcess(
            args, default_rc,
            stdout_map.get(sub, ""), stderr_map.get(sub, ""),
        )
    return fake_run, calls


def test_mint_seed_writes_cloud_init_and_returns_box_id(monkeypatch, tmp_path):
    # provision-seed prints cloud-init to STDOUT and the box_id line to STDERR.
    # mint_seed must capture both via _run_controller_capture_both, write the
    # stdout to --cloud-init-out, parse box_id from stderr.
    fake_run, calls = _fake_run_factory(
        stdout_map={"provision-seed": "#cloud-config\n# fake bundle\n"},
        stderr_map={"provision-seed":
                    "provision-seed: created box_id=b-abc123\n"})
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    out = tmp_path / "x.yaml"
    box_id, path = ru_bringup.mint_seed(
        "selectel", "ru-msk-1",
        canary=True,
        agent_source_url="https://b2/agent.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        cloud_init_out=str(out),
    )
    assert box_id == "b-abc123"
    assert path == str(out)
    assert out.read_text().startswith("#cloud-config")
    assert (out.stat().st_mode & 0o777) == 0o600
    argv = calls[0]
    assert argv[0] == "provision-seed"
    assert "--canary" in argv
    assert "selectel" in argv and "ru-msk-1" in argv


def test_mint_seed_uses_generous_image_url_ttl(monkeypatch, tmp_path):
    """The mtg image URL baked into the seed has a TTL; the agent fetches it at
    VM boot. The default 1h is hostile to human-in-the-loop provisioning (paste
    cloud-init, create VM, boot can take longer), so the box dies with an expired
    download URL. mint_seed must request a generous TTL (>= 24h)."""
    fake_run, calls = _fake_run_factory(
        stdout_map={"provision-seed": "#cloud-config\n"},
        stderr_map={"provision-seed": "provision-seed: created box_id=b-ttl\n"})
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    ru_bringup.mint_seed(
        "timeweb", "ru-moscow-1", canary=False,
        agent_source_url="u", agent_source_sha256="d",
        descriptor_refresh_url="r", cloud_init_out=str(tmp_path / "ci.yaml"))
    argv = calls[0]
    assert "--ttl-seconds" in argv
    ttl = int(argv[argv.index("--ttl-seconds") + 1])
    assert ttl >= 86400


def test_mint_seed_defaults_cloud_init_path_when_omitted(monkeypatch):
    """Regression: cmd_ru_bringup passes args.cloud_init_out=None when the
    operator omits --cloud-init-out. mint_seed must default to
    /tmp/ru-cloud-init-<box_id>.yaml (the path the --help text promises),
    not crash with Path(None). Discovered 2026-06-02 on a real ru-bringup
    run — every operator who didn't pass --cloud-init-out hit it."""
    fake_run, _calls = _fake_run_factory(
        stdout_map={"provision-seed": "#cloud-config\n"},
        stderr_map={"provision-seed":
                    "provision-seed: created box_id=b-zzz999\n"})
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    box_id, path = ru_bringup.mint_seed(
        "timeweb", "ru-msk-1", canary=False,
        agent_source_url="https://b2/agent.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        # cloud_init_out omitted entirely → must default, not crash.
    )
    assert box_id == "b-zzz999"
    assert path == "/tmp/ru-cloud-init-b-zzz999.yaml"
    assert Path(path).read_text().startswith("#cloud-config")
    Path(path).unlink(missing_ok=True)


def test_mint_seed_reclaims_box_when_post_commit_step_fails(monkeypatch, tmp_path):
    """provision-seed commits the box + cover-domain consumption before mint_seed
    finishes. If a later step crashes (the c1a72a8d class: Path(None) after
    commit), mint_seed must auto-reclaim the just-minted box so the operator is
    not left with an orphan holding a cover domain — then re-raise the original
    error."""
    fake_capture, _ = _fake_run_factory(
        stdout_map={"provision-seed": "#cloud-config\n"},
        stderr_map={"provision-seed": "provision-seed: created box_id=b-orphan\n"})
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_capture, raising=False)
    reclaim_run, calls = _fake_run_factory()
    monkeypatch.setattr(ru_bringup, "_run_controller", reclaim_run, raising=False)
    # Force the post-commit cloud-init write to fail (parent dir missing).
    bad = tmp_path / "nope" / "x.yaml"
    with pytest.raises(OSError):
        ru_bringup.mint_seed(
            "selectel", "ru-msk-1", canary=False,
            agent_source_url="u", agent_source_sha256="d",
            descriptor_refresh_url="r", cloud_init_out=str(bad))
    reclaim_calls = [c for c in calls if c and c[0] == "ru-box-reclaim"]
    assert reclaim_calls, "expected ru-box-reclaim to be invoked on post-commit failure"
    assert "b-orphan" in reclaim_calls[0]


def test_mint_seed_does_not_reclaim_on_success(monkeypatch, tmp_path):
    """A clean mint must never reclaim — the box is a legitimate pending provision."""
    fake_capture, _ = _fake_run_factory(
        stdout_map={"provision-seed": "#cloud-config\n"},
        stderr_map={"provision-seed": "provision-seed: created box_id=b-ok\n"})
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_capture, raising=False)
    other_run, calls = _fake_run_factory()
    monkeypatch.setattr(ru_bringup, "_run_controller", other_run, raising=False)
    box_id, _path = ru_bringup.mint_seed(
        "selectel", "ru-msk-1", canary=False,
        agent_source_url="u", agent_source_sha256="d",
        descriptor_refresh_url="r", cloud_init_out=str(tmp_path / "x.yaml"))
    assert box_id == "b-ok"
    assert [c for c in calls if c and c[0] == "ru-box-reclaim"] == []


def test_mark_live_invokes_controller(monkeypatch):
    fake_run, calls = _fake_run_factory()
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    ru_bringup.mark_live("b-abc", "1.2.3.4")
    assert calls[0][0] == "ru-box-mark-live"
    assert "b-abc" in calls[0] and "--public-ip" in calls[0] and "1.2.3.4" in calls[0]
    assert "--db-path" in calls[0]


def test_box_info_parses_ru_box_list_json(monkeypatch):
    rows = [{"box_id": "b-abc", "state": "provisioning", "sni": "cover.example"}]
    fake_run, _ = _fake_run_factory(stdout_map={"ru-box-list": json.dumps(rows)})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    info = ru_bringup.box_info("b-abc")
    assert info["state"] == "provisioning"
    assert info["sni"] == "cover.example"


def test_box_info_returns_none_when_missing(monkeypatch):
    fake_run, _ = _fake_run_factory(stdout_map={"ru-box-list": "[]"})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    assert ru_bringup.box_info("b-missing") is None


def test_wait_for_soak_exits_when_passed(monkeypatch):
    payloads = [
        json.dumps({"passed": False, "reasons": ["canary B below threshold"]}),
        json.dumps({"passed": True, "reasons": []}),
    ]
    def fake_run(*args, check=True, capture=False, env=None):
        return subprocess.CompletedProcess(args, 0, payloads.pop(0), "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup.time, "sleep", lambda s: None)

    progress, writes = [], []
    result = ru_bringup.wait_for_soak(
        "iv-v1", poll_interval_s=0,
        on_progress=lambda reasons: progress.append(list(reasons)),
        state_writer=lambda: writes.append(1),
    )
    assert result.passed is True
    assert result.duration_s >= 0
    assert progress[0] == ["canary B below threshold"]
    assert len(writes) >= 1  # state_writer called at least once during the loop


def test_wait_for_soak_propagates_keyboard_interrupt(monkeypatch):
    def fake_run(*args, **kw):
        return subprocess.CompletedProcess(args, 0,
            json.dumps({"passed": False, "reasons": ["pending"]}), "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    def kc(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(ru_bringup.time, "sleep", kc)

    writes = []
    with pytest.raises(KeyboardInterrupt):
        ru_bringup.wait_for_soak(
            "iv-v1", poll_interval_s=0,
            on_progress=lambda r: None,
            state_writer=lambda: writes.append(1),
        )
    assert writes  # state was saved before the interrupt propagated


def test_cycle_state_round_trip(tmp_path):
    state = ru_bringup.CycleState(
        release="v1.0.0", image_version="iv-v1.0.0",
        profile_path="/tmp/p.json", image_built=True,
        canaries=[{"box_id": "b-1", "provider": "selectel",
                   "region": "ru-msk-1", "public_ip": "1.2.3.4",
                   "marked_live_at": "2026-05-28T12:00:00Z"}],
        started_at="2026-05-28T11:00:00Z",
    )
    p = tmp_path / "v1.0.0.json"
    state.save(p)
    loaded = ru_bringup.CycleState.load(p)
    assert loaded == state


def test_cycle_state_load_missing_returns_none(tmp_path):
    assert ru_bringup.CycleState.load(tmp_path / "absent.json") is None


def test_parse_cohort_from_flags():
    targets = ru_bringup.parse_cohort(
        flags=["provider=selectel,region=ru-msk-1",
               "provider=timeweb,region=ru-spb-1"],
        file_path=None, expected_count=2,
    )
    assert [(t.provider, t.region) for t in targets] == [
        ("selectel", "ru-msk-1"), ("timeweb", "ru-spb-1"),
    ]


def test_parse_cohort_count_mismatch_raises():
    with pytest.raises(ValueError, match="canaries=3"):
        ru_bringup.parse_cohort(
            flags=["provider=selectel,region=ru-msk-1"],
            file_path=None, expected_count=3,
        )


def test_parse_cohort_from_yaml_like_file(tmp_path):
    # File format: simple "key=value" lines per target, one target per line,
    # to avoid a YAML dep. (Spec O-D9: YAML alternative, but stdlib is enough.)
    f = tmp_path / "cohort.txt"
    f.write_text("provider=selectel,region=ru-msk-1\n"
                 "provider=firstvds,region=ru-spb-1\n")
    targets = ru_bringup.parse_cohort(flags=None, file_path=f, expected_count=2)
    assert len(targets) == 2 and targets[0].provider == "selectel"


def _bringup_args(tmp_path, **over):
    base = dict(
        provider="selectel", region="ru-msk-1", canary=True,
        agent_source_url="https://b2/a.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        cloud_init_out=str(tmp_path / "ci.yaml"),
        public_ip="1.2.3.4",     # skip the input() prompt
        box_id=None,
        reach_timeout=1,
        non_interactive=True,
        verbose=False, quiet=True, dry_run=False,
        config=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_ru_bringup_happy_path(monkeypatch, tmp_path):
    box_state = {"v": "provisioning"}
    def fake_run(*args, check=True, capture=False, env=None):
        sub = args[0]
        if sub == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-c1\n")
        if sub == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c1", "state": box_state["v"],
                             "sni": "cover.example"}]), "")
        if sub == "ru-box-mark-live":
            box_state["v"] = "live"
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable",
                        lambda *a, **kw: True)
    monkeypatch.setattr(ru_bringup, "_ensure_image", lambda args: None)

    rc = ru_bringup.cmd_ru_bringup(_bringup_args(tmp_path))
    assert rc == 0
    assert box_state["v"] == "live"


def test_resolve_descriptor_url_explicit_wins(monkeypatch):
    """An explicit --descriptor-refresh-url is used verbatim, no publish."""
    def boom(*a, **k):
        raise AssertionError("must not publish when a URL is given")
    monkeypatch.setattr(ru_bringup, "_run_controller", boom, raising=False)
    args = argparse.Namespace(descriptor_refresh_url="https://given/url", config=None)
    assert ru_bringup._resolve_descriptor_url(args) == "https://given/url"


def test_resolve_descriptor_url_auto_publishes_when_absent(monkeypatch):
    """No --descriptor-refresh-url → run descriptor-publish-now and parse the URL
    from its stdout, so the operator never hand-copies a presigned URL."""
    fake_run, calls = _fake_run_factory(
        stdout_map={"descriptor-publish-now":
                    "descriptor-publish-now: published generation 9 (blob 300 bytes)\n"
                    "https://s3.example/descriptors/current?sig=abc&Expires=1\n"})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    args = argparse.Namespace(descriptor_refresh_url=None, config=None)
    url = ru_bringup._resolve_descriptor_url(args)
    assert url == "https://s3.example/descriptors/current?sig=abc&Expires=1"
    assert "descriptor-publish-now" in [c[0] for c in calls]


def test_ensure_image_skips_when_promoted(monkeypatch):
    fake_run, calls = _fake_run_factory(
        stdout_map={"image-current": json.dumps({"image_version": "iv-x"})})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    ru_bringup._ensure_image(argparse.Namespace(config=None))
    assert "image-current" in [c[0] for c in calls]


def test_ensure_image_runs_prepare_when_none(monkeypatch):
    fake_run, _ = _fake_run_factory(stdout_map={"image-current": "null"})
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    prepared = {}
    def fake_prepare(args):
        prepared["ran"] = True
        return 0
    import mthydra.ops.image_ops as image_ops
    monkeypatch.setattr(image_ops, "cmd_image_prepare", fake_prepare)
    ru_bringup._ensure_image(argparse.Namespace(config=None))
    assert prepared.get("ran") is True


def test_cmd_ru_bringup_mint_mode_requires_provider_and_region(monkeypatch, tmp_path):
    """Mint mode (no --box-id) needs only --provider/--region now (agent +
    descriptor + image auto-resolve). Missing → clean error, exit 2, no mint."""
    calls = []
    def fake_run(*args, **kw):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    args = _bringup_args(tmp_path, box_id=None, provider=None, region=None)
    rc = ru_bringup.cmd_ru_bringup(args)
    assert rc == 2
    assert "provision-seed" not in calls


def test_cmd_ru_bringup_mint_auto_resolves_descriptor(monkeypatch, tmp_path):
    """A mint with provider+region but NO --descriptor-refresh-url succeeds: the
    descriptor URL is auto-published. No presigned URL pasted by the operator."""
    box_state = {"v": "provisioning"}
    def fake_run(*args, check=True, capture=False, env=None):
        sub = args[0]
        if sub == "image-current":
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"image_version": "iv-x"}), "")
        if sub == "descriptor-publish-now":
            return subprocess.CompletedProcess(
                args, 0, "published\nhttps://auto/descriptor?sig=z\n", "")
        if sub == "provision-seed":
            return subprocess.CompletedProcess(
                args, 0, "#cloud-config\n", "provision-seed: created box_id=b-a\n")
        if sub == "ru-box-list":
            return subprocess.CompletedProcess(args, 0, json.dumps(
                [{"box_id": "b-a", "state": box_state["v"], "sni": "c.example"}]), "")
        if sub == "ru-box-mark-live":
            box_state["v"] = "live"
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_resolve_agent",
                        lambda args, cfg=None: ("https://agent/url", "deadbeef"))
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)
    args = _bringup_args(tmp_path, descriptor_refresh_url=None)
    rc = ru_bringup.cmd_ru_bringup(args)
    assert rc == 0
    assert box_state["v"] == "live"


def test_cmd_ru_bringup_resume_skips_mint(monkeypatch, tmp_path):
    calls = []
    def fake_run(*args, **kw):
        calls.append(args[0])
        if args[0] == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-existing", "state": "provisioning",
                             "sni": "cover.example"}]), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)

    rc = ru_bringup.cmd_ru_bringup(_bringup_args(tmp_path, box_id="b-existing"))
    assert rc == 0
    assert "provision-seed" not in calls   # mint skipped on resume


def test_cmd_ru_bringup_aborts_on_unreachable(monkeypatch, tmp_path):
    def fake_run(*args, **kw):
        if args[0] == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-c1\n")
        if args[0] == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c1", "state": "provisioning",
                             "sni": "cover.example"}]), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable",
                        lambda *a, **kw: False)
    monkeypatch.setattr(ru_bringup, "_ensure_image", lambda args: None)

    rc = ru_bringup.cmd_ru_bringup(_bringup_args(tmp_path))
    assert rc != 0   # unreachable → non-zero exit


def _cycle_args(tmp_path, **over):
    base = dict(
        release="v1.0.0",
        profile_json=str(tmp_path / "p.json"),
        canaries=2,
        canary_target=["provider=selectel,region=ru-msk-1",
                       "provider=firstvds,region=ru-spb-1"],
        cohort=None,
        agent_source_url="https://b2/a.tar.gz",
        agent_source_sha256="deadbeef",
        descriptor_refresh_url="https://b2/desc",
        soak_poll=0, soak_timeout=0,
        evidence=None, resume=False,
        non_interactive=True, verbose=False, quiet=True, dry_run=False,
        config=None,
        state_dir=str(tmp_path / "state"),     # tests use a tmp state dir
        promote_yes=True,                       # bypass interactive confirm in tests
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_ru_image_cycle_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "p.json").write_text("{}")
    soak_payloads = [
        json.dumps({"passed": False, "reasons": ["short"]}),
        json.dumps({"passed": True, "reasons": []}),
    ]
    state_ipv4 = iter(["1.1.1.1", "2.2.2.2"])
    minted = iter(["b-c1", "b-c2"])
    promoted = {"v": False}

    def fake_run(*args, check=True, capture=False, env=None):
        sub = args[0]
        if sub == "image-build":
            return subprocess.CompletedProcess(args, 0, "", "")
        if sub == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                f"provision-seed: created box_id={next(minted)}\n")
        if sub == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c1", "state": "live", "sni": "x"},
                            {"box_id": "b-c2", "state": "live", "sni": "y"}]),
                "")
        if sub == "image-promote-status":
            return subprocess.CompletedProcess(args, 0, soak_payloads.pop(0), "")
        if sub == "image-promote":
            promoted["v"] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if sub == "image-current":
            return subprocess.CompletedProcess(args, 0,
                json.dumps({"image_version": "iv-vprev"}), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)
    monkeypatch.setattr(ru_bringup, "_prompt_public_ip",
                        lambda: next(state_ipv4))

    rc = ru_bringup.cmd_ru_image_cycle(_cycle_args(tmp_path))
    assert rc == 0
    assert promoted["v"] is True
    # state file removed on success
    assert not (Path(_cycle_args(tmp_path).state_dir) / "v1.0.0.json").exists()


def test_cmd_ru_image_cycle_resume_skips_built_and_done_canaries(monkeypatch, tmp_path):
    (tmp_path / "p.json").write_text("{}")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pre = ru_bringup.CycleState(
        release="v1.0.0", image_version="iv-v1.0.0",
        profile_path=str(tmp_path / "p.json"), image_built=True,
        canaries=[{"box_id": "b-c1", "provider": "selectel",
                   "region": "ru-msk-1", "public_ip": "1.1.1.1",
                   "marked_live_at": "2026-05-28T12:00:00Z"}],
        started_at="2026-05-28T11:00:00Z",
    )
    pre.save(state_dir / "v1.0.0.json")

    seen_subs = []
    def fake_run(*args, **kw):
        seen_subs.append(args[0])
        if args[0] == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-c2\n")
        if args[0] == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-c2", "state": "live", "sni": "y"}]), "")
        if args[0] == "image-promote-status":
            return subprocess.CompletedProcess(args, 0,
                json.dumps({"passed": True, "reasons": []}), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)
    monkeypatch.setattr(ru_bringup, "_prompt_public_ip", lambda: "2.2.2.2")

    rc = ru_bringup.cmd_ru_image_cycle(
        _cycle_args(tmp_path, state_dir=str(state_dir), resume=True))
    assert rc == 0
    # image-build skipped (already built), and only one provision-seed call
    # (b-c1 already in state with marked_live_at)
    assert seen_subs.count("image-build") == 0
    assert seen_subs.count("provision-seed") == 1


def test_safe_release_rejects_path_traversal():
    # M13 defense-in-depth: --release lands in a filename; refuse traversal.
    with pytest.raises(ValueError, match="--release"):
        ru_bringup._safe_release("../../etc/passwd")
    with pytest.raises(ValueError, match="--release"):
        ru_bringup._safe_release("v1/with/slash")
    with pytest.raises(ValueError, match="--release"):
        ru_bringup._safe_release("")
    # And accepts plausible release tags.
    assert ru_bringup._safe_release("v2.1.7") == "v2.1.7"
    assert ru_bringup._safe_release("v2.1.7-rc1") == "v2.1.7-rc1"


def test_cmd_ru_bringup_auto_fetches_agent_from_manifest(monkeypatch, tmp_path):
    """When --agent-source-url not provided, ru-bringup reads agent.json."""
    from mthydra.ops import agent_ops
    manifest = agent_ops.AgentManifest(
        url="https://auto/agent.tar.gz", sha256="deadbeef" * 8,
        published_at="2026-05-30T00:00:00Z",
        expires_at="2026-06-30T00:00:00Z",
    )
    monkeypatch.setattr(ru_bringup, "_resolve_agent",
                        lambda args, cfg=None: (manifest.url, manifest.sha256))

    box_state = {"v": "provisioning"}
    def fake_run(*args, check=True, capture=False, env=None):
        sub = args[0]
        if sub == "provision-seed":
            return subprocess.CompletedProcess(args, 0, "",
                "provision-seed: created box_id=b-1\n")
        if sub == "ru-box-list":
            return subprocess.CompletedProcess(args, 0,
                json.dumps([{"box_id": "b-1", "state": box_state["v"],
                             "sni": "x"}]), "")
        if sub == "ru-box-mark-live":
            box_state["v"] = "live"
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_bringup, "_run_controller", fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "_run_controller_capture_both",
                        fake_run, raising=False)
    monkeypatch.setattr(ru_bringup, "wait_for_reachable", lambda *a, **kw: True)
    monkeypatch.setattr(ru_bringup, "_ensure_image", lambda args: None)

    args = argparse.Namespace(
        provider="timeweb", region="ru-msk-1", canary=False,
        agent_source_url=None, agent_source_sha256=None,
        descriptor_refresh_url="https://desc",
        cloud_init_out=str(tmp_path / "ci.yaml"),
        public_ip="1.2.3.4", box_id=None, reach_timeout=1,
        non_interactive=True, verbose=False, quiet=True, dry_run=False,
        config=None, db_path=str(tmp_path / "x.sqlite"),
    )
    rc = ru_bringup.cmd_ru_bringup(args)
    assert rc == 0
