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
    lines = ["\U0001f511 Your Telegram proxy is ready — tap a link to connect:", ""]
    qr: list[tuple[str, bytes]] = []
    for i, b in enumerate(payload.boxes, start=1):
        lines.append(f"Proxy {i}: {b.proxy_url}")
        qr.append((f"Proxy {i}", _qr_png(b.proxy_url)))
    if len(payload.boxes) > 1:
        lines += ["", "Add them all — Telegram falls back automatically if one is down."]
    return RenderedMessage(text="\n".join(lines), qr=tuple(qr))
