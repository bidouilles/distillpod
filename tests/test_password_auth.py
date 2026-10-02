import functools

import pytest
import pytest_asyncio

from config import settings
from routers import auth
from services.password_auth import hash_password, verify_password

PASSWORD = "A long test password 123!"


@functools.lru_cache
def stored_hash():
    return hash_password(PASSWORD)


def test_hash_salt_and_verification():
    first, second = stored_hash(), hash_password(PASSWORD)
    assert first != second
    assert PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong", first)
    with pytest.raises(ValueError):
        verify_password(PASSWORD, "scrypt:999999999:8:1:00:00")
    with pytest.raises(ValueError):
        hash_password("short")


@pytest_asyncio.fixture
async def password_client(client, monkeypatch):
    monkeypatch.setattr(settings, "test_mode", False)
    monkeypatch.setattr(settings, "session_secret", "password-test-session-secret")
    monkeypatch.setattr(settings, "login_username", "admin")
    monkeypatch.setattr(settings, "login_password_hash", stored_hash())
    monkeypatch.setattr(settings, "public_url", "https://pod.example")
    monkeypatch.setattr(auth, "_login_attempts", auth.deque())
    # Secure cookies require an HTTPS client even when using ASGITransport.
    client.base_url = "https://pod.example"
    return client


@pytest.mark.asyncio
async def test_password_session_and_logout(password_client):
    assert (await password_client.get("/podcasts/feed")).status_code == 401
    response = await password_client.post("/auth/password", json={"username": "admin", "password": PASSWORD},
                                          headers={"Origin": "https://pod.example"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert (await password_client.get("/auth/me")).json()["name"] == "admin"
    assert (await password_client.get("/podcasts/feed")).status_code == 200
    await password_client.post("/auth/logout")
    assert (await password_client.get("/podcasts/feed")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("username,password", [("admin", "wrong"), ("unknown", PASSWORD)])
async def test_bad_credentials(password_client, username, password):
    response = await password_client.post("/auth/password", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_login_budget_and_origin(password_client):
    body = {"username": "admin", "password": PASSWORD}
    assert (await password_client.post("/auth/password", json=body, headers={"Origin": "https://evil.example"})).status_code == 403
    auth._login_attempts.extend([auth.time.monotonic()] * 10)
    response = await password_client.post("/auth/password", json=body)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    auth._login_attempts.clear()
    auth._login_attempts.extend([auth.time.monotonic() - 61] * 10)
    assert (await password_client.post("/auth/password", json=body)).status_code == 200


@pytest.mark.asyncio
async def test_disabled_malformed_and_oversized(password_client, monkeypatch):
    body = {"username": "admin", "password": PASSWORD}
    monkeypatch.setattr(settings, "login_password_hash", "")
    assert not (await password_client.get("/auth/methods")).json()["password"]
    assert (await password_client.post("/auth/password", json=body)).status_code == 503
    monkeypatch.setattr(settings, "login_password_hash", "invalid")
    assert (await password_client.post("/auth/password", json=body)).status_code == 503
    assert (await password_client.post("/auth/password", json={"username": "admin", "password": "x" * 1025})).status_code == 422


@pytest.mark.asyncio
async def test_browser_session_does_not_unlock_integration(password_client, monkeypatch):
    monkeypatch.setattr(settings, "integration_api_key", "different-private-key")
    await password_client.post("/auth/password", json={"username": "admin", "password": PASSWORD})
    assert (await password_client.get("/integrations/v1/episodes")).status_code == 401
