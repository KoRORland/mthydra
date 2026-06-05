# K2 Proxy-Link Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the distribution bot deliver a tappable `https://t.me/proxy?…` link + QR image per box instead of raw JSON, with the mtg secret derivation single-sourced so the link can't drift from what the RU box accepts.

**Architecture:** A new `src/mthydra/proxy_link.py` owns the secret/link derivation; `ru_agent/config_gen.py` and the EU `distribution/payload.py` both import it. `build_subset` enriches each box with a `proxy_url`; a new `distribution/render.py` turns a payload into a human message + QR PNGs (via `segno`); the Telegram sink gains `send_photo`; the publisher sends the rendered text + photos instead of JSON (JSON stays in `distribution_log` for audit).

**Tech Stack:** Python 3 stdlib (`hashlib`, `urllib`, `io`), `segno` (new, pure-Python QR → PNG), pytest. Spec: `doc/specs/2026-06-05-K2-proxy-link-rendering.md`.

---

## File Structure

**Create:**
- `src/mthydra/proxy_link.py` — `derive_mtg_secret`, `build_proxy_url`. Single source of the FakeTLS secret + link format.
- `src/mthydra/controller/distribution/render.py` — `RenderedMessage`, `render_user_message`. Payload → human text + QR PNGs.
- `tests/unit/test_proxy_link.py`
- `tests/unit/controller/distribution/test_render.py`

**Modify:**
- `src/mthydra/ru_agent/config_gen.py` — drop private `_derive_mtg_secret`, call the shared module.
- `src/mthydra/controller/distribution/payload.py` — `SubsetBox.proxy_url`; `build_subset` reads `reality_uuid`, skips reality-less boxes, derives `proxy_url`; `payload_to_json` includes it; `hash_subset` unchanged.
- `src/mthydra/controller/distribution/sinks.py` — `TelegramDistributionSink.send_photo` (+ DryRun no-op).
- `src/mthydra/controller/distribution/publisher.py` — `_dispatch` renders + sends text/photos, not JSON.
- `pyproject.toml` — add `segno` to runtime deps.
- `tests/unit/controller/distribution/test_payload.py`, `test_sinks.py`, `test_publisher.py` (update/extend — read each first).
- `CHANGELOG.md`.

---

## Task 1: Shared `proxy_link.py`

**Files:**
- Create: `src/mthydra/proxy_link.py`
- Test: `tests/unit/test_proxy_link.py`

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_proxy_link.py`:

```python
"""Tests for the single-sourced mtg FakeTLS secret + proxy-link derivation."""
from __future__ import annotations

import hashlib

from mthydra import proxy_link


def test_derive_mtg_secret_matches_fakeTLS_formula():
    uuid = "11111111-2222-3333-4444-555555555555"
    sni = "www.cloudflare.com"
    expected = ("ee"
                + hashlib.sha256(uuid.encode()).digest()[:16].hex()
                + sni.encode("utf-8").hex())
    assert proxy_link.derive_mtg_secret(uuid, sni) == expected
    assert proxy_link.derive_mtg_secret(uuid, sni).startswith("ee")


def test_build_proxy_url_shape():
    url = proxy_link.build_proxy_url("185.207.66.216", 443, "eeDEADBEEF")
    assert url == ("https://t.me/proxy?server=185.207.66.216&port=443&secret=eeDEADBEEF")
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: mthydra.proxy_link`):

Run: `python -m pytest tests/unit/test_proxy_link.py -v`

- [ ] **Step 3: Implement** — create `src/mthydra/proxy_link.py`:

```python
"""Single source of the MTProto FakeTLS secret + tg proxy link (spec K2-D1).

Imported by BOTH the RU box config generator (ru_agent.config_gen) and the EU
distribution payload builder, so the link the user receives is always exactly
what the box's mtg accepts. Do not duplicate this formula elsewhere.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlencode


def derive_mtg_secret(reality_uuid: str, sni: str) -> str:
    """mtg FakeTLS secret: `ee` + 16 secret bytes (hex) + cover SNI (hex).

    mtg rejects a bare 16-byte hex digest ("incorrect first byte of secret");
    the `ee` type byte selects FakeTLS. The 16 secret bytes are derived
    deterministically from the box's reality_uuid."""
    secret16 = hashlib.sha256(reality_uuid.encode()).digest()[:16]
    return "ee" + secret16.hex() + sni.encode("utf-8").hex()


def build_proxy_url(public_ip: str, port: int, secret: str) -> str:
    """Clickable Telegram proxy link (https scheme works on mobile + desktop)."""
    q = urlencode({"server": public_ip, "port": port, "secret": secret})
    return f"https://t.me/proxy?{q}"
```

- [ ] **Step 4: Run — expect PASS:**

Run: `python -m pytest tests/unit/test_proxy_link.py -v`

Note: `urlencode` renders `185.207.66.216` unchanged and the query order is
`server,port,secret` (insertion order) — matches the test's literal.

- [ ] **Step 5: Commit:**

```bash
git add src/mthydra/proxy_link.py tests/unit/test_proxy_link.py
git commit -m "feat(proxy-link): single-sourced mtg secret + tg proxy URL

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Refactor `config_gen` onto the shared module (drift guard)

**Files:**
- Modify: `src/mthydra/ru_agent/config_gen.py`
- Test: `tests/unit/test_proxy_link.py` (append the drift-guard test)

- [ ] **Step 1: Append the failing drift-guard test** to `tests/unit/test_proxy_link.py`:

```python
def test_config_gen_uses_shared_secret(monkeypatch):
    """The secret rendered into mtg.toml must equal proxy_link.derive_mtg_secret
    for the same (reality_uuid, sni) — they must never drift."""
    from types import SimpleNamespace
    from mthydra.ru_agent import config_gen

    seed = SimpleNamespace(reality_uuid="abc-123", sni="discord.com")
    toml = config_gen.render_mtg_config(seed, sing_box_socks_port=1080).decode()
    expected = proxy_link.derive_mtg_secret("abc-123", "discord.com")
    assert f'secret = "{expected}"' in toml
```

- [ ] **Step 2: Run — expect PASS already** (config_gen currently computes the same value independently), then we refactor so it stays true *by construction*:

Run: `python -m pytest tests/unit/test_proxy_link.py::test_config_gen_uses_shared_secret -v`
Expected: PASS (pre-refactor the formulas coincide).

- [ ] **Step 3: Refactor `config_gen`.** In `src/mthydra/ru_agent/config_gen.py`: remove the private `_derive_mtg_secret` function entirely, drop the now-unused `import hashlib` (only if nothing else uses it — grep first: `grep -n hashlib src/mthydra/ru_agent/config_gen.py`), add `from mthydra import proxy_link`, and in `render_mtg_config` replace `secret = _derive_mtg_secret(seed)` with:

```python
    secret = proxy_link.derive_mtg_secret(seed.reality_uuid, seed.sni)
```

- [ ] **Step 4: Run config_gen + drift tests — expect PASS:**

Run: `python -m pytest tests/unit/test_proxy_link.py tests/unit/ru_agent/ -v`
Expected: PASS (the rendered `mtg.toml` secret is byte-identical to before; existing config_gen tests still pass).

- [ ] **Step 5: Commit:**

```bash
git add src/mthydra/ru_agent/config_gen.py tests/unit/test_proxy_link.py
git commit -m "refactor(ru-agent): config_gen uses shared proxy_link.derive_mtg_secret

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `payload.py` — `proxy_url` per box, skip reality-less boxes

**Files:**
- Modify: `src/mthydra/controller/distribution/payload.py`
- Test: `tests/unit/controller/distribution/test_payload.py` (read it first; extend)

- [ ] **Step 1: Write failing tests.** First read the existing test file and its DB-setup fixture (`sed -n '1,60p' tests/unit/controller/distribution/test_payload.py`) and reuse its pattern. Append:

```python
def test_build_subset_includes_proxy_url(dist_conn):
    # dist_conn: a fixture that seeds a user in a shard with one live box that
    # HAS reality_uuid + an active onward credential. If the existing file has
    # a helper that seeds a box, reuse it and just ensure reality_uuid is set.
    from mthydra.controller.distribution import payload as P
    from mthydra import proxy_link
    p = P.build_subset(dist_conn, "u1", now="2026-06-05T00:00:00Z")
    assert p is not None and len(p.boxes) == 1
    b = p.boxes[0]
    expected_secret = proxy_link.derive_mtg_secret(b_reality_uuid, b.sni)  # see note
    assert b.proxy_url == proxy_link.build_proxy_url(b.public_ip, b.port, expected_secret)
    assert "https://t.me/proxy?server=" in b.proxy_url


def test_build_subset_skips_box_without_reality_uuid(dist_conn_no_reality):
    from mthydra.controller.distribution import payload as P
    p = P.build_subset(dist_conn_no_reality, "u1", now="2026-06-05T00:00:00Z")
    # box exists + live + has credential, but reality_uuid is NULL -> omitted
    assert p is not None
    assert p.boxes == ()


def test_payload_to_json_carries_proxy_url(dist_conn):
    import json
    from mthydra.controller.distribution import payload as P
    p = P.build_subset(dist_conn, "u1", now="2026-06-05T00:00:00Z")
    doc = json.loads(P.payload_to_json(p))
    assert doc["boxes"][0]["proxy_url"].startswith("https://t.me/proxy?server=")
```

Note on the fixtures: the existing `test_payload.py` already seeds boxes for its
current tests. Reuse that seeding helper. The box it seeds must have
`reality_uuid` set (UPDATE `ru_boxes SET reality_uuid='…'`) for `dist_conn`;
add a sibling fixture `dist_conn_no_reality` that leaves `reality_uuid` NULL.
Capture the seeded reality_uuid into `b_reality_uuid` (module-level constant the
fixture uses) so the assertion can recompute the expected secret.

- [ ] **Step 2: Run — expect FAIL** (`SubsetBox` has no `proxy_url`):

Run: `python -m pytest tests/unit/controller/distribution/test_payload.py -k proxy_url -v`

- [ ] **Step 3: Implement.** In `src/mthydra/controller/distribution/payload.py`:

(a) add import at top: `from mthydra import proxy_link`

(b) add `proxy_url` to `SubsetBox`:
```python
@dataclass(frozen=True)
class SubsetBox:
    box_id: str
    public_ip: str
    port: int
    sni: str
    credential_b64: str
    proxy_url: str
```

(c) in `build_subset`, change the per-box `ru_boxes` read to also fetch
`reality_uuid`, skip the box if it's NULL, and build `proxy_url`. Replace the
`meta = conn.execute(...)` block + the `boxes.append(...)` with:

```python
        meta = conn.execute(
            "SELECT public_ip, sni, reality_uuid FROM ru_boxes "
            "WHERE box_id=? AND state IN ('provisioning','live')",
            (box_id,),
        ).fetchone()
        if meta is None or not meta[0] or not meta[2]:
            # no row, no public_ip, or no reality_uuid -> cannot form a usable
            # client link (K2-D7); skip.
            continue
        public_ip, sni, reality_uuid = meta[0], meta[1], meta[2]
        cred = conn.execute(
            "SELECT credential FROM onward_credentials "
            "WHERE box_id=? AND revoked_at IS NULL "
            "ORDER BY issued_at DESC LIMIT 1",
            (box_id,),
        ).fetchone()
        if cred is None:
            continue
        cred_blob = bytes(cred[0])
        port = _box_port(box_id)
        secret = proxy_link.derive_mtg_secret(reality_uuid, sni)
        boxes.append(SubsetBox(
            box_id=box_id, public_ip=public_ip, port=port, sni=sni,
            credential_b64=base64.b64encode(cred_blob).decode("ascii"),
            proxy_url=proxy_link.build_proxy_url(public_ip, port, secret),
        ))
```

(d) in `payload_to_json`, add `"proxy_url": b.proxy_url,` to each box dict.

(e) **Do NOT change `hash_subset`** (K2-D6) — it must keep hashing only
`box_id|public_ip|sni|credential_b64`.

- [ ] **Step 4: Run — expect PASS** (new + existing payload tests):

Run: `python -m pytest tests/unit/controller/distribution/test_payload.py -v`

If an existing test constructs `SubsetBox(...)` positionally without `proxy_url`,
update those constructions to pass `proxy_url="https://t.me/proxy?server=x&port=443&secret=ee00"` (or similar) — read the failures and fix the call sites.

- [ ] **Step 5: Commit:**

```bash
git add src/mthydra/controller/distribution/payload.py tests/unit/controller/distribution/test_payload.py
git commit -m "feat(distribution): build_subset derives per-box proxy_url; skip reality-less

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `render.py` — human message + QR (segno)

**Files:**
- Modify: `pyproject.toml` (add `segno`)
- Create: `src/mthydra/controller/distribution/render.py`
- Test: `tests/unit/controller/distribution/test_render.py`

- [ ] **Step 1: Add the dependency + install.** In `pyproject.toml`, add `"segno>=1.6"` to the `dependencies = [...]` list (after `cryptography>=44.0.1`,). Then install into the venv:

Run: `pip install segno`
Expected: `Successfully installed segno-…`. Verify: `python -c "import segno; print(segno.__version__)"`.

- [ ] **Step 2: Write failing tests** — create `tests/unit/controller/distribution/test_render.py`:

```python
"""Tests for distribution.render — human message + QR (spec K2)."""
from __future__ import annotations

from mthydra.controller.distribution.payload import SubsetBox, SubsetPayload
from mthydra.controller.distribution import render


def _payload(boxes):
    return SubsetPayload(user_id="u1", shard_id="default_shard",
                         generated_at="2026-06-05T00:00:00Z",
                         boxes=tuple(boxes), subset_hash="h")


def _box(n):
    url = f"https://t.me/proxy?server=10.0.0.{n}&port=443&secret=ee{n:02d}"
    return SubsetBox(box_id=f"b{n}", public_ip=f"10.0.0.{n}", port=443,
                     sni="www.cloudflare.com", credential_b64="x", proxy_url=url)


def test_render_numbers_all_boxes_and_makes_one_qr_each():
    msg = render.render_user_message(_payload([_box(1), _box(2)]))
    assert "Proxy 1" in msg.text and "Proxy 2" in msg.text
    assert "https://t.me/proxy?server=10.0.0.1" in msg.text
    assert "https://t.me/proxy?server=10.0.0.2" in msg.text
    assert len(msg.qr) == 2
    # each qr entry: (caption, png_bytes) with a PNG signature
    for caption, png in msg.qr:
        assert caption.startswith("Proxy ")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_empty_payload_has_no_qr():
    msg = render.render_user_message(_payload([]))
    assert msg.qr == ()
    assert "no prox" in msg.text.lower()
```

- [ ] **Step 3: Run — expect FAIL** (`ModuleNotFoundError: …distribution.render`):

Run: `python -m pytest tests/unit/controller/distribution/test_render.py -v`

- [ ] **Step 4: Implement** — create `src/mthydra/controller/distribution/render.py`:

```python
"""Render a per-user subset into a human Telegram message + QR PNGs (spec K2).

Replaces dumping raw JSON into the user's chat. One numbered tappable link per
box; one QR PNG per box for the read-on-desktop / scan-with-phone case.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import segno

from mthydra.controller.distribution.payload import SubsetPayload


@dataclass(frozen=True)
class RenderedMessage:
    text: str
    qr: tuple[tuple[str, bytes], ...]   # (caption, png_bytes) per box


def _qr_png(data: str) -> bytes:
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


def render_user_message(payload: SubsetPayload) -> RenderedMessage:
    if not payload.boxes:
        return RenderedMessage(
            text="No proxies are assigned to you yet. You'll get a link here "
                 "as soon as one is ready.",
            qr=(),
        )
    lines = ["🔑 Your Telegram proxy is ready — tap a link to connect:", ""]
    qr: list[tuple[str, bytes]] = []
    for i, b in enumerate(payload.boxes, start=1):
        lines.append(f"Proxy {i}: {b.proxy_url}")
        qr.append((f"Proxy {i}", _qr_png(b.proxy_url)))
    if len(payload.boxes) > 1:
        lines += ["", "Add them all — Telegram falls back automatically if one is down."]
    return RenderedMessage(text="\n".join(lines), qr=tuple(qr))
```

- [ ] **Step 5: Run — expect PASS:**

Run: `python -m pytest tests/unit/controller/distribution/test_render.py -v`

- [ ] **Step 6: Commit:**

```bash
git add pyproject.toml src/mthydra/controller/distribution/render.py tests/unit/controller/distribution/test_render.py
git commit -m "feat(distribution): render.py — numbered proxy links + segno QR

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Telegram sink `send_photo`

**Files:**
- Modify: `src/mthydra/controller/distribution/sinks.py`
- Test: `tests/unit/controller/distribution/test_sinks.py` (read first; extend)

- [ ] **Step 1: Write failing test** — append to `tests/unit/controller/distribution/test_sinks.py`:

```python
def test_telegram_send_photo_posts_multipart_sendphoto():
    captured = {}
    def fake_photo(url, fields, png):
        captured["url"] = url
        captured["fields"] = fields
        captured["png_len"] = len(png)
        return 200, '{"ok":true}'
    from mthydra.controller.distribution.sinks import TelegramDistributionSink
    sink = TelegramDistributionSink("TOKEN", http_post_photo=fake_photo)
    res = sink.send_photo(chat_id="123", png=b"\x89PNG......", caption="Proxy 1")
    assert res.success is True
    assert captured["url"].endswith("/sendPhoto")
    assert captured["fields"] == {"chat_id": "123", "caption": "Proxy 1"}
    assert captured["png_len"] == len(b"\x89PNG......")


def test_telegram_send_photo_reports_http_error():
    from mthydra.controller.distribution.sinks import TelegramDistributionSink
    sink = TelegramDistributionSink("TOKEN", http_post_photo=lambda u, f, p: (400, "bad"))
    res = sink.send_photo(chat_id="123", png=b"x", caption="c")
    assert res.success is False and "400" in (res.error or "")
```

- [ ] **Step 2: Run — expect FAIL** (no `http_post_photo` kwarg / no `send_photo`):

Run: `python -m pytest tests/unit/controller/distribution/test_sinks.py -k send_photo -v`

- [ ] **Step 3: Implement.** In `src/mthydra/controller/distribution/sinks.py`, extend `TelegramDistributionSink.__init__` to accept `http_post_photo` and add the method. Update `__init__` signature + body:

```python
    def __init__(
        self,
        bot_token: str,
        http_post: Callable[[str, dict], tuple[int, str]] | None = None,
        http_get: Callable[[str, dict], tuple[int, str]] | None = None,
        http_post_photo: Callable[[str, dict, bytes], tuple[int, str]] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._http_post = http_post or self._default_http_post
        self._http_get = http_get or self._default_http_get
        self._http_post_photo = http_post_photo or self._default_http_post_photo
```

Add these two methods (the default builds multipart/form-data with urllib):

```python
    @staticmethod
    def _default_http_post_photo(url: str, fields: dict, png: bytes) -> tuple[int, str]:
        import urllib.error
        import urllib.request
        import uuid as _uuid

        boundary = _uuid.uuid4().hex
        parts: list[bytes] = []
        for k, v in fields.items():
            parts.append(
                (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\""
                 f"\r\n\r\n{v}\r\n").encode("utf-8"))
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
             f"filename=\"proxy.png\"\r\nContent-Type: image/png\r\n\r\n").encode("utf-8"))
        parts.append(png)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return int(e.code), e.read().decode("utf-8", errors="replace")
        except Exception as e:
            return 0, str(e)

    def send_photo(self, *, chat_id: str, png: bytes, caption: str) -> SinkResult:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendPhoto"
        try:
            status, text = self._http_post_photo(
                url, {"chat_id": chat_id, "caption": caption}, png)
        except Exception as e:
            return SinkResult(sink="telegram", success=False, error=repr(e))
        if 200 <= status < 300:
            return SinkResult(sink="telegram", success=True, error=None)
        return SinkResult(sink="telegram", success=False,
                          error=f"http {status}: {text[:200]}")
```

- [ ] **Step 4: Run — expect PASS:**

Run: `python -m pytest tests/unit/controller/distribution/test_sinks.py -v`

- [ ] **Step 5: Commit:**

```bash
git add src/mthydra/controller/distribution/sinks.py tests/unit/controller/distribution/test_sinks.py
git commit -m "feat(distribution): TelegramDistributionSink.send_photo (sendPhoto multipart)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Publisher sends rendered text + QR, not JSON

**Files:**
- Modify: `src/mthydra/controller/distribution/publisher.py`
- Test: `tests/unit/controller/distribution/test_publisher.py` (read first; extend)

- [ ] **Step 1: Write failing test.** First read `test_publisher.py` to learn how it builds a `DistributionPublisher` with fake sinks (`sed -n '1,80p' tests/unit/controller/distribution/test_publisher.py`). The Telegram fake sink there is currently a callable capturing `message`; extend it with a `send_photo` capturing photos. Add a test asserting: (a) the telegram sink receives the **rendered link text**, not JSON; (b) `send_photo` is called once per box; (c) `distribution_log.payload_json` still stores the JSON.

```python
def test_publisher_sends_rendered_link_not_json(pub_env):
    # pub_env: existing fixture/harness that seeds one user with telegram channel
    # + one live box WITH reality_uuid + credential, and a publisher wired to
    # fake sinks that record calls. Reuse the file's existing setup; add a
    # send_photo recorder to the telegram fake.
    sent = pub_env.run_publish_once()   # however the existing tests trigger a tick
    tg = pub_env.telegram_calls         # list of {"message": ...}
    assert any("https://t.me/proxy?server=" in c["message"] for c in tg)
    assert not any(c["message"].lstrip().startswith("{") for c in tg)  # no JSON
    assert len(pub_env.telegram_photos) == 1                            # one box -> one QR
    # audit record still JSON
    row = pub_env.last_distribution_log_payload()
    assert row.startswith("{") and '"proxy_url"' in row
```

Adapt names to the actual harness in the file. If the file uses a concrete
`TelegramDistributionSink` with injected `http_post`/`http_post_photo`, inject
fakes for both and assert on the captured `sendMessage` body vs `sendPhoto` calls.

- [ ] **Step 2: Run — expect FAIL** (publisher still sends `payload_body` JSON):

Run: `python -m pytest tests/unit/controller/distribution/test_publisher.py -k rendered_link -v`

- [ ] **Step 3: Implement.** In `src/mthydra/controller/distribution/publisher.py`:

(a) add import: `from mthydra.controller.distribution.render import render_user_message`

(b) `payload_body = payload_to_json(payload)` stays (it is still stored in
`distribution_log` via the `_dl.append(..., payload_json=payload_body)` call —
leave that as-is). Compute the rendered message once before the channel loop:

```python
                payload_body = payload_to_json(payload)
                rendered = render_user_message(payload)
```

(c) change `_dispatch` to take the rendered message and use it for delivery
while leaving the stored `payload_json` untouched. New `_dispatch`:

```python
    def _dispatch(
        self,
        channel_label: str,
        configured: str,
        rendered,
        payload,
    ) -> tuple[bool, str | None]:
        sink = (
            self.telegram_sink if channel_label == "telegram"
            else self.email_sink
        )
        if self.mode == "offline":
            sink = _OFFLINE_SINK
        try:
            if channel_label == "telegram":
                res = sink(chat_id=configured, message=rendered.text)
                if getattr(res, "success", False):
                    for caption, png in rendered.qr:
                        sink.send_photo(chat_id=configured, png=png, caption=caption)
            else:
                res = sink(
                    to_addr=configured,
                    subject=(
                        f"mthydra proxy update — {payload.user_id} "
                        f"({len(payload.boxes)} proxies)"
                    ),
                    body=rendered.text,
                )
        except Exception as e:
            return False, repr(e)
        return (bool(getattr(res, "success", False)),
                getattr(res, "error", None))
```

(d) update the call site to pass `rendered` instead of `payload_body`:
`success, err = self._dispatch(channel_label, configured, rendered, payload)`

(e) ensure `_OFFLINE_SINK` has a no-op `send_photo`. Find its definition
(`grep -n "_OFFLINE_SINK" src/mthydra/controller/distribution/publisher.py`); if
it's a DryRun class instance, add `def send_photo(self, **kw): return SinkResult(sink="offline", success=True, error=None)` to that class (or a lambda-bearing shim). Read the actual definition and match its shape.

- [ ] **Step 4: Run — expect PASS** (publisher + full distribution suite):

Run: `python -m pytest tests/unit/controller/distribution/ -v`
Expected: PASS. Fix any test that asserted the old JSON delivery (update it to expect the rendered link — that's the intended behavior change, not a regression to paper over).

- [ ] **Step 5: Commit:**

```bash
git add src/mthydra/controller/distribution/publisher.py tests/unit/controller/distribution/test_publisher.py
git commit -m "feat(distribution): publisher delivers rendered link + QR, not raw JSON

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Regression + docs

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Full suite.** Run `python -m pytest -q --ignore=tests/integration/test_gap_monitor.py`. Expected: pass except the 3 known-pre-existing `box has no shard` failures (`test_cover_pool_lifecycle`, `test_image_canary_lifecycle`, `test_cover_pool_invariants`) — those are unrelated to this work. Any NEW failure must be fixed.

- [ ] **Step 2: Lint the touched files.** Run `ruff check --select I,F src/mthydra/proxy_link.py src/mthydra/controller/distribution/render.py src/mthydra/controller/distribution/payload.py src/mthydra/controller/distribution/sinks.py src/mthydra/controller/distribution/publisher.py src/mthydra/ru_agent/config_gen.py`. Expected: clean. (Note: a blanket `ruff check src/` reports ~165 pre-existing phantom errors from the local ruff 0.15 vs pinned `>=0.5` — ignore those; only `I`/`F` on touched files matter.)

- [ ] **Step 3: CHANGELOG.** Under `## Unreleased — 2026-06-05`, add:

```markdown
**Granny-usable proxy links (spec K2).** The distribution bot now delivers a
tappable `https://t.me/proxy?…` link + QR image per box instead of raw JSON.
The mtg FakeTLS secret derivation is single-sourced in `mthydra.proxy_link`
(shared by the RU box's `config_gen` and the EU payload builder) so the link
always matches what the box accepts. New runtime dependency: `segno` (pure-Python
QR). The internal payload keeps its structured fields (now incl. `proxy_url`) for
audit; `subset_hash` is unchanged. Boxes without a `reality_uuid` are omitted
from a user's delta (they can't form a usable link).
```

- [ ] **Step 4: Commit + push:**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG for K2 proxy-link rendering

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push origin main
```

---

## Self-Review (completed during planning)

**Spec coverage:** K2-D1 → Tasks 1+2 (shared module + config_gen refactor + drift test). K2-D2 → Tasks 3 (payload gains proxy_url) + 6 (human delivery, not JSON). K2-D3 (`https://t.me/proxy`) → Task 1 `build_proxy_url`. K2-D4 (all boxes numbered + QR each) → Task 4 `render`. K2-D5 (`segno`) → Task 4 step 1. K2-D6 (`subset_hash` unchanged) → Task 3 step 3(e). K2-D7 (skip reality-less) → Task 3 step 3(c). K2-D8 (JSON to audit only) → Task 6 (payload_json still stored; humans get rendered text). Testing §5 → tests in Tasks 1–6.

**Placeholder scan:** the only non-literal bits are the test-fixture adaptations in Tasks 3 & 6, which explicitly instruct reading the existing test harness and reusing its seeding helper — necessary because those fixtures already exist and must not be duplicated blindly. All production code is shown in full.

**Type consistency:** `derive_mtg_secret(reality_uuid, sni)` / `build_proxy_url(public_ip, port, secret)` used identically in Tasks 1, 2, 3. `SubsetBox.proxy_url` defined in Task 3, consumed in Tasks 4 & 6. `RenderedMessage(text, qr)` defined in Task 4, consumed in Task 6. `send_photo(*, chat_id, png, caption)` defined in Task 5, called in Task 6.
