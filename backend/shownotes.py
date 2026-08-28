"""
Episode descriptions ("about this episode") as plain text for model context.

Feeds ship these as HTML, often substantial: guest names, their handles, links
to what was discussed, sponsor URLs, and a timestamped outline. That is exactly
the material a listener asks follow-up questions about ("what was the guest's
blog?"), and none of it is spoken aloud, so the transcript alone cannot answer.

Stripping tags outright would throw away every URL, since they live in href
attributes rather than the link text. So links are flattened to "text (url)".
"""
import html
import re

# Cap what we inject. Lex Fridman's notes run to several thousand characters of
# sponsor links; past a point they crowd out the transcript in the prompt.
MAX_CHARS = 4000

_LINK = re.compile(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_BREAK = re.compile(r'</?(?:br|p|div|li|tr|h[1-6])\b[^>]*>', re.I)
_TAG = re.compile(r'<[^>]+>')
_BLANKS = re.compile(r'\n{3,}')
_SPACES = re.compile(r'[ \t]{2,}')


def to_text(description: str | None, max_chars: int = MAX_CHARS) -> str:
    """Flatten an HTML episode description to readable plain text."""
    if not description:
        return ""

    def link(m: re.Match) -> str:
        url, text = m.group(1).strip(), _TAG.sub("", m.group(2)).strip()
        if not text:
            return url
        # Don't write "https://x (https://x)" when the label is already the URL.
        if text.rstrip("/") == url.rstrip("/"):
            return url
        return f"{text} ({url})"

    text = _LINK.sub(link, description)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANKS.sub("\n\n", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n…(show notes truncated)"
    return text
