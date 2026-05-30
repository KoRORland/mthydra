from __future__ import annotations

import subprocess

from mthydra.controller.probe_runner import probers


def _stub_ssh(returncode, stdout, stderr=""):
    def fn(*cmd_parts, timeout_s=30):
        return subprocess.CompletedProcess(
            ("ssh",) + tuple(cmd_parts), returncode, stdout, stderr)
    return fn


_OPENSSL_OK = """\
CONNECTED(00000003)
depth=2 C = US, O = ...
verify return:1
---
Certificate chain
 0 s:CN = www.cloudflare.com
   i:C = US, O = Cloudflare, Inc., CN = Cloudflare Inc ECC CA-3
---
Server certificate
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
subject=/CN=www.cloudflare.com
issuer=/C=US/O=Cloudflare, Inc./CN=Cloudflare Inc ECC CA-3
---
SSL handshake has read 3201 bytes and written 388 bytes
Verification: OK
Verify return code: 0 (ok)
---
"""

_OPENSSL_BAD = (
    "CONNECTED\nverify error:num=20:unable to get local issuer cert"
    "\nVerify return code: 20\n"
)


def test_tls_fall_through_pass_on_verified_handshake():
    status, evidence = probers.probe_tls_fall_through(
        _stub_ssh(0, _OPENSSL_OK), "1.2.3.4", "www.cloudflare.com")
    assert status == "pass"
    assert "Verify return code: 0" in evidence


def test_tls_fall_through_hard_fail_on_verify_error():
    status, evidence = probers.probe_tls_fall_through(
        _stub_ssh(0, _OPENSSL_BAD), "1.2.3.4", "www.cloudflare.com")
    assert status == "hard_fail"


def test_tls_fall_through_soft_fail_on_ssh_failure():
    status, _ = probers.probe_tls_fall_through(
        _stub_ssh(255, "", "ssh: connect timeout"),
        "1.2.3.4", "www.cloudflare.com")
    assert status == "soft_fail"


_NC_443_ONLY = """\
nc: connect to 1.2.3.4 80 (tcp) failed: Connection refused
Ncat: Connected to 1.2.3.4:443.
Ncat: 0 bytes sent, 0 bytes received in 0.01 seconds.
nc: connect to 1.2.3.4 8080 (tcp) failed: Connection refused
nc: connect to 1.2.3.4 22 (tcp) failed: Connection refused
nc: connect to 1.2.3.4 53 (tcp) failed: Connection refused
"""

_NC_EXTRA_PORT = _NC_443_ONLY.replace(
    "nc: connect to 1.2.3.4 22 (tcp) failed: Connection refused",
    "Ncat: Connected to 1.2.3.4:22.",
)


def test_surface_scan_pass_on_443_only():
    status, _ = probers.probe_surface_scan(
        _stub_ssh(0, _NC_443_ONLY), "1.2.3.4")
    assert status == "pass"


def test_surface_scan_hard_fail_on_extra_open_port():
    status, evidence = probers.probe_surface_scan(
        _stub_ssh(0, _NC_EXTRA_PORT), "1.2.3.4")
    assert status == "hard_fail"
    assert "22" in evidence


def test_cover_consistency_pass_when_issuers_match():
    box_out = "issuer=/C=US/O=Cloudflare, Inc./CN=Cloudflare Inc ECC CA-3\n"
    cover_out = box_out
    calls = []
    def stub(*cmd_parts, timeout_s=30):
        calls.append(list(cmd_parts))
        out = box_out if "1.2.3.4:443" in " ".join(cmd_parts) else cover_out
        return subprocess.CompletedProcess(cmd_parts, 0, out, "")
    status, _ = probers.probe_cover_consistency(stub, "1.2.3.4",
                                                "www.cloudflare.com")
    assert status == "pass"


def test_cover_consistency_hard_fail_on_mismatch():
    def stub(*cmd_parts, timeout_s=30):
        if "1.2.3.4:443" in " ".join(cmd_parts):
            return subprocess.CompletedProcess(cmd_parts, 0,
                "issuer=/C=US/O=Cloudflare, Inc./CN=Cloudflare Inc ECC CA-3\n", "")
        return subprocess.CompletedProcess(cmd_parts, 0,
            "issuer=/CN=SuspiciousCA\n", "")
    status, _ = probers.probe_cover_consistency(stub, "1.2.3.4",
                                                "www.cloudflare.com")
    assert status == "hard_fail"
