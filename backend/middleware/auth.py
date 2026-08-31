import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from fastapi.responses import JSONResponse, RedirectResponse
from jose import jwt, JWTError
from config import settings

# Only these API prefixes require a valid session.
# Everything else (frontend SPA, static files, auth routes) passes through freely.
#
# /chat and /research were missing here: both were reachable without a session
# on a public deployment, letting anyone read episode conversations, post
# messages that spend the owner's model subscription, and queue research jobs.
#
# Every new router belongs on this list. A route that is merely not listed is
# not "unprotected pending review" — it is open, and reads or writes the
# owner's library. /storage in particular can delete media.
PROTECTED_PREFIXES = [
    "/gists", "/podcasts", "/player", "/chat", "/research", "/tags", "/search",
    "/youtube", "/queue", "/bookmarks", "/playlists", "/storage",
]


def create_session_token(user: dict) -> str:
    payload = {
        "email": user["email"],
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
        "exp": int(time.time()) + settings.session_max_age,
    }
    return jwt.encode(payload, settings.session_secret, algorithm="HS256")


def verify_session_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=["HS256"])
        return payload
    except JWTError:
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # TEST_MODE: bypass auth entirely — E2E only, never in prod
        if settings.test_mode:
            request.state.user = {
                "email": "test@example.com",
                "name": "Test User",
                "picture": "",
            }
            return await call_next(request)

        path = request.url.path

        # Only protect API routes — frontend, auth, and static files pass freely
        if not any(path.startswith(p) for p in PROTECTED_PREFIXES):
            return await call_next(request)

        # Validate session cookie
        token = request.cookies.get("distillpod_session")
        if not token:
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(url="/unauthorized", status_code=302)
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        user = verify_session_token(token)
        if not user:
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(url="/unauthorized", status_code=302)
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        request.state.user = user
        return await call_next(request)
