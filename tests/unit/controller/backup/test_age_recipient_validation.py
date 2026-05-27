"""Pure-Python bech32 validation of age recipients (H4).

No `age` binary required — exercises validate_recipient's checksum logic
directly, including the silent-corruption case the prefix check missed.
"""
import pytest

from mthydra.controller.backup.age_crypt import AgeError, validate_recipient

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

# Canonical age example recipient (valid bech32, HRP "age").
GOOD = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frm, to):
    acc = bits = 0
    ret = []
    maxv = (1 << to) - 1
    for value in data:
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            ret.append((acc >> bits) & maxv)
    if bits:
        ret.append((acc << (to - bits)) & maxv)
    return ret


def _encode_age_recipient(pubkey: bytes) -> str:
    data = _convertbits(list(pubkey), 8, 5)
    polymod = _polymod(_hrp_expand("age") + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return "age1" + "".join(_CHARSET[d] for d in data + checksum)


def test_accepts_canonical_recipient():
    validate_recipient(GOOD)  # must not raise


def test_accepts_freshly_encoded_recipient():
    rec = _encode_age_recipient(bytes(range(32)))
    validate_recipient(rec)


def test_rejects_mutated_checksum():
    """Flip one data char -> checksum no longer matches -> loud failure."""
    rec = _encode_age_recipient(bytes(range(32)))
    # Mutate a char in the data region (after 'age1', before the last 6).
    i = 6
    orig = rec[i]
    repl = _CHARSET[(_CHARSET.index(orig) + 1) % 32]
    mutated = rec[:i] + repl + rec[i + 1:]
    assert mutated != rec
    with pytest.raises(AgeError, match="checksum"):
        validate_recipient(mutated)


def test_rejects_truncated_recipient():
    with pytest.raises(AgeError):
        validate_recipient(GOOD[:-1])


def test_rejects_wrong_hrp():
    # Valid bech32 but HRP is not "age".
    data = _convertbits(list(bytes(range(32))), 8, 5)
    polymod = _polymod(_hrp_expand("bc") + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    rec = "bc1" + "".join(_CHARSET[d] for d in data + checksum)
    with pytest.raises(AgeError):
        validate_recipient(rec)


def test_rejects_non_age_prefix():
    with pytest.raises(AgeError, match="invalid age recipient"):
        validate_recipient("not-an-age-key")


def test_rejects_bad_char():
    with pytest.raises(AgeError):
        validate_recipient("age1" + "b" * 50 + "io1")  # 'i','o' not in charset
