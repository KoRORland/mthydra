# Spec K2 — User-Facing Proxy-Link Rendering

## 1. Purpose

Close the gap between design.md §45 ("reduces to a single `tg://proxy?…` link /
QR — trivial onboarding, no extra software, no settings") and what the
distribution channel actually delivers today: the raw spec-K subset JSON,
pasted verbatim into the user's Telegram chat. A non-technical user ("granny")
receives a developer-facing JSON blob and has no usable artifact.

This amendment makes the distribution bot deliver a **tappable
`https://t.me/proxy?…` link plus a QR image** per box, derived from the same
secret the RU box's mtg actually accepts.

## 2. Background — why the JSON is not enough today

A Telegram client needs `server` + `port` + `secret` to connect. The mtg
FakeTLS secret is derived on the box (`ru_agent/config_gen._derive_mtg_secret`):

```
secret = "ee" + sha256(reality_uuid).digest()[:16].hex() + sni.encode().hex()
```

The spec-K delta carries `public_ip`, `port`, `sni`, and `credential_b64` — but
`credential_b64` is the control-plane `onward_credential` (box authorization),
**not** this secret, and the delta omits `reality_uuid` entirely. So the payload
as shipped cannot even be converted into a working link by any downstream
helper. The EU controller *does* hold `reality_uuid` (`ru_boxes.reality_uuid`,
schema v6), so it can derive the secret and emit the link directly.

## 3. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| K2-D1 | **Single-source the secret/link derivation** in a new top-level module `src/mthydra/proxy_link.py`. `ru_agent/config_gen.py` is refactored to import it; its private `_derive_mtg_secret` is removed. | This is the structural fix for K-D3's original worry ("format may drift from upstream"). Box and link now share one definition; a test asserts they agree, so they cannot silently diverge. |
| K2-D2 | **Revise K-D3.** The internal subset payload keeps its structured fields and *gains* a derived `proxy_url`; the **human delivery** is a rendered link + QR, no longer raw JSON. | K-D3's format-agnostic payload was right for storage/audit; it was wrong to also be the *user-facing* artifact. The two concerns are now separated. |
| K2-D3 | **Deliver a tappable `https://t.me/proxy?server=&port=&secret=` link** (not the `tg://` scheme). | `https://t.me/proxy` is clickable on phone *and* desktop Telegram and opens the in-app proxy add; `tg://` is less universally clickable outside a mobile client. |
| K2-D4 | **All boxes, numbered** (`Proxy 1: <link>`, `Proxy 2: …`) + one **QR image per box**. | Operator-chosen. Telegram retains multiple proxies and fails over; listing all is the most resilient for the user. QR covers the read-on-desktop / scan-with-phone case. |
| K2-D5 | **QR via `segno`** (new runtime dependency). | Pure-Python, tiny, writes PNG directly with no Pillow. Deliberately avoids `qrcode`+Pillow — Pillow is the kind of heavy dependency this project rejects (cf. paramiko in P-D5). |
| K2-D6 | **`subset_hash` is unchanged** — still over box identity (`box_id\|public_ip\|sni\|credential_b64`). `proxy_url` is derived, not new state, so it is excluded from the hash. | Keeps dedup/resend behavior identical. A box whose `reality_uuid` (and thus secret) changes already triggers a resend via credential rotation. |
| K2-D7 | **A box with no `reality_uuid` is skipped from the delta**, with an audit row — same pattern as boxes with no active credential. | Such a box cannot produce a client-usable FakeTLS link; surfacing a linkless box to a user is worse than omitting it. |
| K2-D8 | **Raw JSON is no longer sent to any user channel**, but is still written to `distribution_log.payload_json` for audit/dedup. | The machine record stays for operators/forensics; the human just gets the link(s) + QR. |

## 4. Components

**New: `src/mthydra/proxy_link.py`**
```python
def derive_mtg_secret(reality_uuid: str, sni: str) -> str:
    # "ee" + sha256(reality_uuid)[:16].hex() + sni.encode("utf-8").hex()

def build_proxy_url(public_ip: str, port: int, secret: str) -> str:
    # https://t.me/proxy?server=<ip>&port=<port>&secret=<secret>
```
Imported by both `ru_agent/config_gen.py` (replacing `_derive_mtg_secret`) and
`controller/distribution/payload.py`.

**Modified: `controller/distribution/payload.py`**
- `build_subset` also selects `reality_uuid` from `ru_boxes`; a box with NULL
  `reality_uuid` is skipped (K2-D7, audited by the caller).
- `SubsetBox` gains `proxy_url: str`; `hash_subset` is **unchanged** (K2-D6).
- `payload_to_json` includes `proxy_url` in each box dict (audit record).

**New: `controller/distribution/render.py`**
```python
@dataclass(frozen=True)
class RenderedMessage:
    text: str                          # numbered links + instructions
    qr: tuple[tuple[str, bytes], ...]  # (caption, png_bytes) per box

def render_user_message(payload: SubsetPayload) -> RenderedMessage: ...
```
`text` lists every box as `Proxy N: <proxy_url>`; `qr` carries one segno-PNG per
box, captioned `Proxy N`. Empty-box payloads render a short "no proxies assigned
yet" message with no QR.

**Modified: `controller/distribution/sinks.py`**
- Telegram sink gains `send_photo(chat_id, png: bytes, caption: str)`
  (`sendPhoto` multipart POST).

**Modified: `controller/distribution/publisher.py`**
- For the telegram channel: send `RenderedMessage.text`, then one `send_photo`
  per `qr` entry. For email: send `RenderedMessage.text` (links; no QR).
- `_dispatch` no longer passes the raw JSON body to user channels; the stored
  `payload_json` (audit/dedup) is still `payload_to_json(payload)`.

**Modified: `pyproject.toml`** — add `segno` to runtime `dependencies`.

## 5. Testing

- `proxy_link.derive_mtg_secret` produces **exactly** the string
  `ru_agent/config_gen` produces for the same `(reality_uuid, sni)` — the
  drift-guard test (build a fake seed, compare). After the refactor,
  `config_gen` calls the shared function, so this stays true by construction.
- `build_subset`: a box with `reality_uuid` set gets a `proxy_url` of the
  expected shape; a box with NULL `reality_uuid` is omitted; `subset_hash`
  matches the pre-change value for the same identity fields (K2-D6).
- `render_user_message`: N boxes → N numbered links in `text` and N QR entries;
  empty payload → no-proxy text, zero QR.
- Telegram sink `send_photo`: correct `sendPhoto` URL + multipart shape (mocked
  transport).
- Publisher: telegram delivery calls send-text then send-photo(s) and does
  **not** send raw JSON; `distribution_log.payload_json` still holds the JSON;
  dedup on unchanged `subset_hash` still skips.

## 6. Out of scope

- Per-box port storage (still `:443` constant per spec-K `_box_port`).
- Email QR attachments (email gets text links only).
- Any change to the RU data plane or the secret formula itself.
