"""The SPA's own paths must not be claimed by an API route.

The frontend is served by the same app as the API, through a catch-all that
runs last. So the moment an API router registers a path the SPA also uses, a
hard navigation or a reload of that screen returns JSON instead of the app —
and only on a real reload, which is exactly the case nobody clicks through
while developing.

This happened: adding `/queue` and `/playlists` as routers took `/queue` and
`/playlists/:id` away from the SPA, which had used both for months. Hence a
test rather than a note.
"""
import pytest

# Every path the router in frontend/src/App.tsx renders a page for, with the
# parameterised ones filled in.
SPA_PATHS = [
    "/",
    "/search",
    "/library",
    "/subscriptions",
    "/subscriptions/pod-123",
    "/library/playlists/abc-123",
    "/player/ep-123",
    "/player/ep-123/chat",
    "/up-next",
    "/saved",
    "/unauthorized",
]


CATCH_ALL = "/{full_path:path}"


def _api_routes():
    """Every API route except the catch-all that serves the SPA itself.

    `include_router` no longer flattens into `app.routes`: since FastAPI 0.140
    each inclusion appears as a wrapper holding the original router, so this
    has to walk into them. Reading only the top level silently sees four routes
    and passes every assertion below — which is why
    `test_enumeration_actually_finds_the_api` exists.
    """
    import main
    from fastapi.routing import APIRoute

    found = []

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                if r.path != CATCH_ALL:
                    found.append(r)
                continue
            inner = getattr(r, "original_router", None)
            if inner is not None:
                walk(inner.routes)

    walk(main.app.routes)
    return found


def test_enumeration_actually_finds_the_api():
    """Guards the guard: an empty list would make every test here vacuous."""
    paths = {r.path for r in _api_routes()}
    for expected in ("/queue", "/bookmarks", "/playlists/{playlist_id}", "/podcasts/feed"):
        assert expected in paths, f"route enumeration missed {expected}"


@pytest.mark.parametrize("path", SPA_PATHS)
def test_spa_path_is_not_shadowed_by_an_api_route(path):
    clashes = [
        f"{sorted(r.methods)} {r.path}"
        for r in _api_routes()
        if r.path_regex.match(path) and "GET" in (r.methods or set())
    ]
    assert not clashes, (
        f"{path} is served by the API, so reloading that screen returns JSON "
        f"instead of the app: {clashes}"
    )


def test_the_spa_catch_all_is_registered_last():
    """Ordering is what makes every other route reachable at all."""
    import main
    assert getattr(main.app.routes[-1], "path", None) == CATCH_ALL
