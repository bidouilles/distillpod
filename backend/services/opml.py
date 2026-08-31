"""OPML import and export — the way a podcast library moves between apps.

This is the only feature here whose whole value is that other software
understands it, so it stays deliberately plain: `<outline type="rss">` rows
with `xmlUrl` and `text`, which is what every podcast app actually reads.

YouTube subscriptions are exported too, with their channel page as the URL.
Nothing else can subscribe to those, but an export is also a backup of this
library, and silently dropping half of it would make it a poor one.
"""
import logging
import re
from datetime import datetime, timezone
from xml.etree import ElementTree
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)


def build(subscriptions: list[dict], title: str = "DistillPod subscriptions") -> str:
    """Render subscriptions as an OPML document."""
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{escape(title)}</title>",
        f"    <dateCreated>{now}</dateCreated>",
        "  </head>",
        "  <body>",
    ]
    for sub in subscriptions:
        feed_url = (sub.get("feed_url") or "").strip()
        if not feed_url:
            continue
        attrs = [
            'type="rss"',
            f'text="{escape(sub.get("title") or "Untitled", {chr(34): "&quot;"})}"',
            f'xmlUrl="{escape(feed_url, {chr(34): "&quot;"})}"',
        ]
        lines.append(f"    <outline {' '.join(attrs)} />")
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)


def parse(xml: str | bytes) -> list[dict]:
    """Feeds found in an OPML document, as `{title, feed_url}`.

    Tolerant on purpose: exports in the wild nest outlines inside category
    folders, omit `type`, use `url` instead of `xmlUrl`, and occasionally
    arrive with a byte-order mark. A row without a URL is skipped rather than
    failing the whole import, because one bad line should not cost the other
    forty subscriptions.
    """
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8-sig", errors="replace")
    xml = xml.lstrip("﻿ \t\r\n")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"not a readable OPML file: {exc}") from exc

    found: list[dict] = []
    seen: set[str] = set()
    # iter() rather than a body/outline path: category folders nest outlines
    # arbitrarily deep, and every real export uses them.
    for node in root.iter("outline"):
        url = (node.get("xmlUrl") or node.get("xmlurl") or node.get("url") or "").strip()
        if not url or not re.match(r"^https?://", url, re.I):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = (node.get("text") or node.get("title") or "").strip()
        found.append({"title": title or url, "feed_url": url})
    return found
