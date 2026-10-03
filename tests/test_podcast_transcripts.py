import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from database import get_db
from services import jobs, podcast_transcripts as publisher, rss, transcriber


def test_rss_preserves_all_transcript_alternatives():
    xml = '''<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>
      <item><guid>ep_001</guid><podcast:transcript url="https://example.com/a.txt" type="text/plain"/>
      <podcast:transcript url="https://example.com/a.vtt" type="text/vtt" language="fr"/></item>
      <item><guid>ep_002</guid></item></channel></rss>'''
    links = rss.transcript_links(xml)
    assert len(links["ep_001"]) == 2
    assert links["ep_001"][1]["language"] == "fr"
    assert links["ep_002"] == []


@pytest.mark.parametrize("mime,text", [
    ("text/vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\nBonjour <b>tout</b> le monde"),
    ("application/x-subrip", "1\n00:00:01,000 --> 00:00:03,000\nBonjour tout le monde"),
    ("application/json", '{"segments":[{"startTime":1,"endTime":3,"body":"Bonjour tout le monde"}]}'),
])
def test_supported_formats_keep_text_and_cue_boundaries(mime, text):
    words = publisher.parse_timed(text, mime)
    assert "".join(w["word"] for w in words).strip() == "Bonjour tout le monde"
    assert words[0]["start"] == 1
    assert words[-1]["end"] == 3
    assert all(w["start"] <= w["end"] for w in words)


@pytest.mark.parametrize("text", ["WEBVTT\n\n", "00:03.000 --> 00:01.000\nBad", "NaN:00 --> 01:00\nBad"])
def test_invalid_timing_is_not_imported(text):
    with pytest.raises(ValueError):
        publisher.parse_timed(text, "text/vtt")


async def set_sources(links):
    db = await get_db()
    try:
        await db.execute("UPDATE episodes SET transcript_sources=? WHERE id='ep_001'", (json.dumps(links),))
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_publisher_transcript_prevents_stt_and_is_searchable(client, monkeypatch, tmp_path):
    await set_sources([{"url": "https://example.com/a.vtt", "type": "text/vtt", "language": "fr"}])
    monkeypatch.setattr(publisher, "fetch_text", AsyncMock(return_value="00:01.000 --> 00:03.000\nBonjour monde"))
    monkeypatch.setattr(transcriber.stt, "transcribe", lambda _: pytest.fail("STT called"))
    jobs.reset()
    jobs.set_lock_dir(tmp_path)
    try:
        words, language = await transcriber.obtain_words("ep_001", Path("unused.mp3"))
        assert language == "fr" and len(words) == 2
        # A later request must work even when the publisher becomes unreachable.
        monkeypatch.setattr(publisher, "obtain", AsyncMock(side_effect=RuntimeError("unreachable")))
        again, language = await transcriber.obtain_words("ep_001", Path("unused.mp3"))
        assert again == words and language == "fr"
        db = await get_db()
        try:
            hit = await db.execute_fetchone("SELECT episode_id FROM transcripts_fts WHERE transcripts_fts MATCH 'Bonjour'")
            assert hit["episode_id"] == "ep_001"
        finally:
            await db.close()
    finally:
        jobs.reset()


@pytest.mark.asyncio
async def test_parallel_requests_run_stt_once(client, monkeypatch, tmp_path):
    await set_sources([])
    calls = []
    def transcribe(_, **kwargs):
        calls.append(1)
        return [{"word": " hello", "start": 0, "end": 1}]
    monkeypatch.setattr(transcriber.stt, "transcribe", transcribe)
    jobs.reset()
    jobs.set_lock_dir(tmp_path)
    try:
        await asyncio.gather(*(transcriber.obtain_words("ep_001", Path("unused")) for _ in range(2)))
        assert len(calls) == 1
    finally:
        jobs.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("links", [
    [{"url":"https://example.com/a.txt", "type":"text/plain"}],
    [{"url":"https://example.com/a.vtt", "type":"text/vtt"}],
])
async def test_existing_but_unusable_transcript_never_triggers_stt(client, monkeypatch, links):
    await set_sources(links)
    monkeypatch.setattr(publisher, "fetch_text", AsyncMock(side_effect=RuntimeError("429")))
    monkeypatch.setattr(transcriber.stt, "transcribe", lambda _: pytest.fail("STT called"))
    with pytest.raises((ValueError, RuntimeError)):
        await transcriber._obtain_words("ep_001", Path("unused"))


@pytest.mark.asyncio
async def test_unknown_source_is_discovered_before_stt(client, monkeypatch):
    monkeypatch.setattr(publisher, "fetch_text", AsyncMock(return_value='<rss><channel><item><guid>ep_001</guid></item></channel></rss>'))
    words, _ = await publisher.obtain("ep_001")
    assert words == []
    db = await get_db()
    try:
        assert (await db.execute_fetchone("SELECT transcript_sources FROM episodes WHERE id='ep_001'"))[0] == "[]"
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:pass@example.com/a", "http://127.0.0.1/a", "http://169.254.169.254/a"])
async def test_private_transcript_urls_are_rejected(url):
    with pytest.raises(ValueError):
        await publisher._public_url(url)


@pytest.mark.asyncio
async def test_failed_recreation_preserves_existing_transcript(client, monkeypatch, tmp_path):
    old = [{"word":" original", "start":0, "end":1}]
    db = await get_db()
    try:
        await transcriber.store_transcript(db, "ep_001", old)
    finally:
        await db.close()
    await set_sources([])
    def fail(*args, **kwargs):
        raise RuntimeError("Model download failed")
    monkeypatch.setattr(transcriber.stt, "transcribe", fail)
    jobs.reset()
    jobs.set_lock_dir(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="download failed"):
            await transcriber.obtain_words("ep_001", Path("unused"), force=True)
        assert await transcriber._stored_words("ep_001") == old
        response = (await client.get("/player/transcript-status/ep_001")).json()
        assert response["status"] == "error" and "download failed" in response["error"]
    finally:
        jobs.reset()
