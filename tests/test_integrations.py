"""External tools must see cached evidence without acquiring session privileges."""
import json
import sqlite3
from xml.etree import ElementTree as ET

import pytest
import pytest_asyncio

from config import settings
from conftest import EPISODE_ID_1, GIST_ID
from middleware.auth import create_session_token

pytestmark = pytest.mark.asyncio
BASE = "/integrations/v1"
KEY = "test-integration-key"
HEADERS = {"Authorization": f"Bearer {KEY}"}


@pytest_asyncio.fixture
async def external_client(client, monkeypatch):
    monkeypatch.setattr(settings, "integration_api_key", KEY)
    monkeypatch.setattr(settings, "test_mode", False)
    monkeypatch.setattr(settings, "public_url", "https://pod.example")
    return client


def execute(db_path, sql, params=()):
    with sqlite3.connect(db_path) as db:
        db.execute(sql, params)


@pytest.mark.parametrize("path", ["/episodes", "/distillations", "/feed.rss", "/openapi.json",
                                      f"/episodes/{EPISODE_ID_1}", f"/episodes/{EPISODE_ID_1}/transcript"])
async def test_all_routes_require_key(external_client, path):
    response = await external_client.get(BASE + path, headers={"Accept": "text/html"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "private, no-store"


async def test_disabled_and_wrong_keys(external_client, monkeypatch):
    assert (await external_client.get(BASE + "/episodes", headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await external_client.get(BASE + "/episodes", params={"api_key": KEY})).status_code == 401
    monkeypatch.setattr(settings, "integration_api_key", "")
    assert (await external_client.get(BASE + "/episodes", headers=HEADERS)).status_code == 503


async def test_session_and_test_mode_do_not_open_feed(external_client, monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "test-session-secret")
    external_client.cookies.set("distillpod_session", create_session_token({"email": "test@example.com"}))
    assert (await external_client.get(BASE + "/episodes")).status_code == 401
    monkeypatch.setattr(settings, "test_mode", True)
    assert (await external_client.get(BASE + "/episodes")).status_code == 401


async def test_integration_key_cannot_use_app_routes_or_write(external_client):
    assert (await external_client.get("/gists/", headers=HEADERS)).status_code == 401
    assert (await external_client.post("/player/play", headers=HEADERS, json={"episode_id": EPISODE_ID_1})).status_code == 401
    assert (await external_client.post(BASE + "/episodes", headers=HEADERS)).status_code == 405


async def test_latest_episode_pages_and_allowlist(external_client):
    response = await external_client.get(BASE + "/episodes", headers=HEADERS, params={"limit": 2})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization"
    data = response.json()
    assert [r["id"] for r in data["items"]] == ["ep_003", "ep_002"]
    assert data["next_offset"] == 2
    last = (await external_client.get(BASE + "/episodes", headers=HEADERS, params={"limit": 2, "offset": 2})).json()
    assert last["next_offset"] is None
    ep = last["items"][0]
    assert ep["id"] == EPISODE_ID_1
    assert ep["distillation_count"] == 1
    assert ep["ai_summary"] is None
    assert ep["links"]["transcript"].startswith("https://pod.example/integrations/v1/")
    assert not {"local_path", "adfree_path", "position", "played", "words_json", "description"} & ep.keys()


async def test_publication_dates_use_actual_time_not_lexical_order(external_client, tmp_db):
    execute(tmp_db, "UPDATE episodes SET published_at = ?, created_at = ? WHERE id = 'ep_001'",
            ("2026-02-03T01:00:00+02:00", "2099-01-01T00:00:00Z"))
    data = (await external_client.get(BASE + "/episodes", headers=HEADERS,
                                    params={"published_since": "2026-02-03T00:00:00Z"})).json()
    assert [r["id"] for r in data["items"]] == ["ep_003"]


async def test_shared_title_and_status_filters(external_client):
    data = (await external_client.get(BASE + "/episodes", headers=HEADERS,
                                    params={"q": "One", "status": "distilled"})).json()
    assert [r["id"] for r in data["items"]] == [EPISODE_ID_1]
    assert (await external_client.get(BASE + "/episodes", headers=HEADERS, params={"q": "' OR 1=1 --"})).json()["items"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1},
                                     {"published_since": "garbage"}, {"status": "bogus"}, {"q": "x" * 201}])
async def test_invalid_episode_inputs(external_client, params):
    assert (await external_client.get(BASE + "/episodes", headers=HEADERS, params=params)).status_code == 422


async def test_distillations_preserve_evidence_and_since(external_client):
    data = (await external_client.get(BASE + "/distillations", headers=HEADERS,
                                    params={"episode_id": EPISODE_ID_1})).json()
    assert data["items"][0]["id"] == GIST_ID
    assert data["items"][0]["text"] == "Some transcribed text"
    assert data["items"][0]["summary"] == "AI summary"
    assert data["items"][0]["start_seconds"] == 60
    assert (await external_client.get(BASE + "/distillations", headers=HEADERS,
                                     params={"created_since": "2026-02-02"})).json()["items"] == []


async def test_cached_episode_detail(external_client, tmp_db):
    execute(tmp_db, "UPDATE episodes SET summary = 'Cached AI summary', local_path = '/private/file' WHERE id = ?", (EPISODE_ID_1,))
    execute(tmp_db, "INSERT INTO episode_notes VALUES (?, ?, ?)", (EPISODE_ID_1, '{"key_points":["A"]}', "2026-02-01"))
    execute(tmp_db, "INSERT INTO researches (id, gist_id, episode_id, status, created_at, report_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("r1", GIST_ID, EPISODE_ID_1, "done", "2026-02-01", '{"claim":"A claim"}'))
    response = await external_client.get(f"{BASE}/episodes/{EPISODE_ID_1}", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["ai_summary"] == "Cached AI summary"
    assert data["ai_notes"] == {"key_points": ["A"]}
    assert data["research_reports"][0]["report"] == {"claim": "A claim"}
    assert "/private/file" not in response.text
    assert (await external_client.get(BASE + "/episodes/missing", headers=HEADERS)).status_code == 404


async def test_transcript_paging_and_missing(external_client, tmp_db):
    words = [{"word": " Hello", "start": 0, "end": 0.3},
             {"word": " world", "start": 0.4, "end": 0.8}, {"word": "!", "start": 1, "end": 1.1}]
    execute(tmp_db, "INSERT INTO transcripts VALUES (?, ?, ?, ?)", (EPISODE_ID_1, json.dumps(words), "en", "2026-02-01"))
    path = f"{BASE}/episodes/{EPISODE_ID_1}/transcript"
    first = (await external_client.get(path, headers=HEADERS, params={"limit": 2})).json()
    assert first["text"] == "Hello world"
    assert first["timeline"] == "original"
    assert first["next_offset"] == 2
    assert first["total_words"] == 3
    last = (await external_client.get(path, headers=HEADERS, params={"limit": 2, "offset": 2})).json()
    assert first["words"] + last["words"] == words
    assert last["next_offset"] is None
    assert (await external_client.get(path, headers=HEADERS, params={"limit": 5001})).status_code == 422
    assert (await external_client.get(f"{BASE}/episodes/ep_002/transcript", headers=HEADERS)).status_code == 404


async def test_corrupt_transcript_fails_honestly(external_client, tmp_db):
    execute(tmp_db, "INSERT INTO transcripts VALUES (?, ?, ?, ?)", (EPISODE_ID_1, "not-json", "en", "2026-02-01"))
    response = await external_client.get(f"{BASE}/episodes/{EPISODE_ID_1}/transcript", headers=HEADERS)
    assert response.status_code == 500
    assert response.json()["detail"] == "Stored transcript is invalid"


@pytest.mark.parametrize("kind", ["episodes", "distillations"])
async def test_rss_valid_xml_and_escaping(external_client, tmp_db, kind):
    execute(tmp_db, "UPDATE episodes SET title = ?, summary = ? WHERE id = ?", ("AI <news> & trends\x01", "<script>bad & text</script>", EPISODE_ID_1))
    execute(tmp_db, "UPDATE gists SET summary = ? WHERE id = ?", ("Quote <tag> & text\x01", GIST_ID))
    response = await external_client.get(BASE + "/feed.rss", headers=HEADERS, params={"kind": kind})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    root = ET.fromstring(response.content)
    assert root.attrib["version"] == "2.0"
    channel = root.find("channel")
    assert channel.findtext("link") == "https://pod.example"
    assert len(channel.findall("item")) == (3 if kind == "episodes" else 1)
    for item in channel.findall("item"):
        assert item.find("guid").attrib["isPermaLink"] == "false"
        assert item.findtext("pubDate").endswith("GMT")
    assert root.find(".//script") is None
    descriptions = [item.findtext("description") for item in channel.findall("item")]
    assert any("&lt;" in description for description in descriptions)


async def test_schema_only_contains_readonly_integration_operations(external_client):
    response = await external_client.get(BASE + "/openapi.json", headers=HEADERS)
    assert response.status_code == 200
    schema = response.json()
    assert schema["servers"] == [{"url": "https://pod.example"}]
    assert schema["components"]["securitySchemes"]["IntegrationKey"]["scheme"] == "bearer"
    for path, operations in schema["paths"].items():
        assert path.startswith(BASE + "/")
        assert set(operations) == {"get"}
        assert operations["get"]["security"] == [{"IntegrationKey": []}]
    assert "TranscriptPage" in schema["components"]["schemas"]
