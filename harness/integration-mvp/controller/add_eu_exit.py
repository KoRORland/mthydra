#!/usr/bin/env python3
"""Register one EU exit carrying a real Reality pubkey, then sign a descriptor.

The MVP quickstart treats the controller host itself as the EU data-exit; wiring
the full data_exit Reality keygen is out of scope for this integration harness.
We inject one exit exactly the way the proven agent-boot harness (make_seed.py)
does — a real x25519 Reality pubkey so sing-box's `public_key` validation passes
— and sign the descriptor over it.

Usage: add_eu_exit.py <db_path> <cover_sni>
"""
import base64
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_exit_set import add_exit, list_active
from mthydra.descriptor.sign import sign_new_descriptor

NOW = "2026-06-08T00:00:00Z"
VALID_UNTIL = "2026-07-08T00:00:00Z"


def main() -> int:
    db_path, cover_sni = sys.argv[1], sys.argv[2]
    conn = connect(db_path)
    if list_active(conn):
        print("[eu-exit] exit already present → skip")
    else:
        raw = X25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        reality_pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        add_exit(conn, "harness-eu-fp", "192.0.2.1:443", 1, NOW,
                 cover_sni=cover_sni, reality_pubkey=reality_pub)
        print(f"[eu-exit] added harness-eu-fp (cover_sni={cover_sni})")
    gen, _, _ = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
    conn.commit()
    print(f"[eu-exit] signed descriptor gen={gen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
