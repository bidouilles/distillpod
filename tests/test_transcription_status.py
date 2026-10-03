from unittest.mock import patch

import pytest

from database import get_db
from services import transcription_state

pytestmark = pytest.mark.asyncio


async def test_status_reports_progress_and_failure_reason(client):
    db = await get_db()
    try:
        await db.execute("""UPDATE episodes SET transcript_status='processing',
            transcript_progress=42, transcript_stage='Transcribing audio', transcript_error=NULL
            WHERE id='ep_001'""")
        await db.commit()
        response = (await client.get('/player/transcript-status/ep_001')).json()
        assert response['progress_percent'] == 42
        assert response['stage'] == 'Transcribing audio'
        await db.execute("UPDATE episodes SET transcript_status='error', transcript_error='Caption download was rate-limited' WHERE id='ep_001'")
        await db.commit()
        response = (await client.get('/player/transcript-status/ep_001')).json()
        assert response['error'] == 'Caption download was rate-limited'
    finally:
        await db.close()


async def test_retry_starts_server_job_and_does_not_require_playback(client):
    with patch('routers.player._start_download') as download:
        response = await client.post('/player/transcribe/ep_001')
        assert response.status_code == 202
        assert response.json()['status'] == 'queued'
        assert download.call_count == 1
        response = await client.post('/player/transcribe/ep_001')
        assert response.status_code == 202
        assert download.call_count == 1  # a second click does not enqueue twice


async def test_activity_includes_transcription_from_another_process(client):
    db = await get_db()
    try:
        await db.execute("UPDATE episodes SET transcript_status='processing' WHERE id='ep_001'")
        await db.commit()
    finally:
        await db.close()
    response = (await client.get('/player/jobs')).json()
    assert 'Episode One' in response['stt']['running']


async def test_progress_is_persisted_and_late_updates_do_not_replace_completion(client):
    await transcription_state.update('ep_001', 'processing', 'Preparing audio')
    report = transcription_state.reporter('ep_001')
    report(35, 'Transcribing audio')
    assert (await client.get('/player/transcript-status/ep_001')).json()['progress_percent'] == 35
    await transcription_state.update('ep_001', 'done', 'Transcript ready')
    report(50, 'A late callback')
    assert (await client.get('/player/transcript-status/ep_001')).json()['progress_percent'] == 100
