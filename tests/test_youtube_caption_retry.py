from unittest.mock import AsyncMock, patch
import asyncio
from pathlib import Path

import pytest

from database import get_db
from services.transcriber import check_youtube_captions

pytestmark = pytest.mark.asyncio


async def seed_youtube():
    db = await get_db()
    try:
        await db.execute("UPDATE episodes SET id='yt-testvideo' WHERE id='ep_003'")
        await db.commit()
    finally:
        await db.close()


async def test_check_can_find_captions_later_without_speech_or_audio(client):
    await seed_youtube()
    words = [{"word": " Hello", "start": 0.0, "end": 1.0}]
    with patch('services.transcriber.youtube.fetch_metadata', new_callable=AsyncMock) as metadata, \
         patch('services.transcriber.youtube.fetch_caption_words', new_callable=AsyncMock) as captions, \
         patch('services.transcriber.youtube.caption_language', return_value='en'), \
         patch('services.transcriber.stt.transcribe') as speech:
        captions.side_effect = [[], words]
        await check_youtube_captions('yt-testvideo')
        status = (await client.get('/player/transcript-status/yt-testvideo')).json()
        assert status['status'] == 'none'
        assert 'not available yet' in status['stage']
        await check_youtube_captions('yt-testvideo')
        assert metadata.await_count == 2  # absence is never cached
        assert (await client.get('/player/transcript-status/yt-testvideo')).json()['status'] == 'done'
        speech.assert_not_called()
    db = await get_db()
    try:
        assert await db.execute_fetchone("SELECT * FROM transcripts_fts WHERE episode_id='yt-testvideo'")
    finally:
        await db.close()


async def test_caption_failure_is_visible_and_does_not_fall_back(client):
    await seed_youtube()
    with patch('services.transcriber.youtube.fetch_metadata', side_effect=RuntimeError('YouTube rate limited')), \
         patch('services.transcriber.stt.transcribe') as speech:
        with pytest.raises(RuntimeError):
            await check_youtube_captions('yt-testvideo')
        status = (await client.get('/player/transcript-status/yt-testvideo')).json()
        assert status['status'] == 'error'
        assert status['error'] == 'YouTube rate limited'
        speech.assert_not_called()


async def test_caption_route_deduplicates_and_never_downloads(client):
    await seed_youtube()
    with patch('routers.player._start_transcription') as start, \
         patch('routers.player._start_download') as download:
        for _ in range(2):
            response = await client.post('/player/transcribe/yt-testvideo?captions_only=true')
            assert response.status_code == 202
        start.assert_called_once()
        assert start.call_args.kwargs['captions_only'] is True
        download.assert_not_called()
    assert (await client.post('/player/transcribe/ep_001?captions_only=true')).status_code == 400


async def test_manual_audio_transcription_is_explicit(client):
    await seed_youtube()
    with patch('routers.player._start_download') as download:
        assert (await client.post('/player/transcribe/yt-testvideo')).status_code == 202
        assert download.call_args.kwargs['captions_only'] is False


async def test_automatic_youtube_work_only_checks_captions(client):
    from routers.player import _start_transcription
    with patch('routers.player.check_youtube_captions', new_callable=AsyncMock) as captions, \
         patch('routers.player.transcribe_episode', new_callable=AsyncMock) as speech:
        _start_transcription('yt-testvideo', Path('/unused'))
        await asyncio.sleep(0)
        captions.assert_awaited_once_with('yt-testvideo')
        speech.assert_not_awaited()
