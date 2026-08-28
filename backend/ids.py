"""
Episode identifiers.

An episode's id comes from its RSS <guid>, which publishers are free to set to
anything — and several use a URL. Lex Fridman's feed, for instance, emits
`https://lexfridman.com/?p=6506`. That id is also a path segment in both the
API (`/player/episode/{episode_id}`) and the SPA (`/player/:episodeId`), so a
guid containing `/` or `?` silently breaks routing: the route matches only up
to the first slash and the page never loads.

Percent-encoding the link does not help. uvicorn decodes the path before
Starlette routes it, so `%2F` is back to `/` by the time it matters. The id
itself has to be URL-safe, so we derive one here.
"""
import hashlib
import re

# Unreserved characters (RFC 3986) — safe in a path segment with no encoding.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def is_url_safe(episode_id: str) -> bool:
    return bool(_SAFE_ID.match(episode_id))


def safe_episode_id(guid: str) -> str:
    """Return a URL-safe, stable id for an RSS guid.

    Already-safe guids pass through untouched, so ids minted before this existed
    keep working and their transcripts, gists and chapters stay attached.
    Anything else collapses to a hash of the guid — deterministic, so re-syncing
    a feed maps an episode onto the same row rather than duplicating it.
    """
    if is_url_safe(guid):
        return guid
    return hashlib.sha1(guid.encode("utf-8")).hexdigest()[:32]
