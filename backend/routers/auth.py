import secrets
import asyncio
import time
from collections import deque
from urllib.parse import urlencode
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, RedirectResponse
from authlib.integrations.httpx_client import AsyncOAuth2Client
from config import settings
from middleware.auth import create_session_token, verify_session_token
from services.password_auth import verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
REDIRECT_URI = f"{settings.public_url}/auth/google/callback"

# One owner, one uvicorn worker: a global budget avoids forwarded-IP spoofing
# and caps expensive hash work regardless of the reverse proxy's address.
_login_attempts: deque[float] = deque()
_password_slots = asyncio.Semaphore(2)


class PasswordLogin(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


@router.get("/methods")
async def login_methods():
    return {"password": bool(settings.login_password_hash),
            "google": bool(settings.google_client_id and settings.google_client_secret)}


@router.post("/password")
async def password_login(body: PasswordLogin, request: Request):
    if not settings.login_password_hash or not settings.session_secret:
        raise HTTPException(503, "Password login is not configured")
    # JSON-only input plus same-origin checks prevent cross-site login CSRF.
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in {settings.public_url.rstrip("/"), settings.frontend_origin.rstrip("/")}:
        raise HTTPException(403, "Invalid login origin")
    now = time.monotonic()
    while _login_attempts and _login_attempts[0] <= now - 60:
        _login_attempts.popleft()
    if len(_login_attempts) >= 10:
        raise HTTPException(429, "Too many login attempts. Try again in one minute.", headers={"Retry-After": "60"})
    _login_attempts.append(now)
    try:
        async with _password_slots:
            valid = await asyncio.to_thread(verify_password, body.password, settings.login_password_hash)
    except ValueError:
        raise HTTPException(503, "Password login is not configured correctly")
    # Always do the hash before checking the username; avoid a cheap user oracle.
    if not valid or not secrets.compare_digest(body.username.encode(), settings.login_username.encode()):
        raise HTTPException(401, "Invalid username or password")
    user = {"email": settings.login_username, "name": settings.login_username, "picture": ""}
    response = JSONResponse(user, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        "distillpod_session", create_session_token(user), max_age=settings.session_max_age,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return response


@router.get("/google")
async def google_login():
    """Redirect user to Google OAuth2 consent screen."""
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    }
    url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    response = RedirectResponse(url=url)
    # Store state in short-lived cookie for CSRF validation
    response.set_cookie(
        "oauth_state", state,
        max_age=600,  # 10 minutes
        httponly=True, secure=True, samesite="lax",
    )
    return response


@router.get("/google/callback")
async def google_callback(request: Request, code: str, state: str):
    """Handle Google OAuth2 callback, validate, set session cookie."""
    # CSRF: validate state matches what we stored
    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        return JSONResponse({"detail": "Invalid OAuth state"}, status_code=400)

    # Exchange code for access token
    async with AsyncOAuth2Client(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=REDIRECT_URI,
    ) as client:
        await client.fetch_token(GOOGLE_TOKEN_URL, code=code)
        resp = await client.get(GOOGLE_USERINFO_URL)
        userinfo = resp.json()

    email = userinfo.get("email", "")
    allowed = [e.strip() for e in settings.allowed_emails.split(",") if e.strip()]
    if email not in allowed:
        return JSONResponse({"detail": "Access denied"}, status_code=403)

    user = {
        "email": email,
        "name": userinfo.get("name", ""),
        "picture": userinfo.get("picture", ""),
    }

    session_token = create_session_token(user)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        "distillpod_session", session_token,
        max_age=settings.session_max_age,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    response.delete_cookie("oauth_state")
    return response


@router.get("/me")
async def get_me(request: Request):
    """Return current logged-in user from session cookie, or 401."""
    # TEST_MODE bypasses auth on every protected API route, so gating the SPA
    # behind a login wall here just means a wide-open backend behind a door
    # that cannot be opened. Report the same synthetic user the middleware
    # already injects, so TEST_MODE means one thing rather than two.
    if settings.test_mode:
        return {"email": "test@example.com", "name": "Test User", "picture": ""}

    token = request.cookies.get("distillpod_session")
    if not token:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    user = verify_session_token(token)
    if not user:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return {
        "email": user.get("email"),
        "name": user.get("name"),
        "picture": user.get("picture"),
    }


@router.post("/logout")
async def logout():
    """Clear the session cookie."""
    response = JSONResponse({"ok": True})
    response.delete_cookie("distillpod_session", path="/")
    return response


@router.post("/test-session")
async def test_session():
    """
    TEST_MODE ONLY — set a valid session cookie without going through Google OAuth.
    Used by Playwright E2E global setup. Returns 404 in production (test_mode=False).
    """
    if not settings.test_mode:
        return JSONResponse({"detail": "Not found"}, status_code=404)

    user = {"email": "test@example.com", "name": "Test User", "picture": ""}
    token = create_session_token(user)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "distillpod_session", token,
        max_age=settings.session_max_age,
        httponly=True,
        secure=False,   # False for http://localhost in E2E
        samesite="lax",
        path="/",
    )
    return response


@router.get("/debug-cookie")
async def debug_cookie(request: Request):
    """Debug endpoint — only available in test mode."""
    from fastapi.responses import JSONResponse
    if not settings.test_mode:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    from middleware.auth import verify_session_token
    token = request.cookies.get("distillpod_session")
    user = verify_session_token(token) if token else None
    return {"token_present": bool(token), "token_valid": bool(user)}
