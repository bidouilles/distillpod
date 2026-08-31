"""Media retention — the housekeeping that keeps a small VPS alive.

The risk here is not a bug in a query, it is deleting audio someone still
wanted, so the tests are mostly about what must *not* be touched: anything
queued, anything half-heard, anything unplayed, and — always — the transcript.
"""
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3

pytestmark = pytest.mark.asyncio


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _stale(path, hours: int = 3):
    """Backdate a file, so it is not mistaken for a download in flight."""
    old = time.time() - hours * 3600
    os.utime(path, (old, old))
    return path


@pytest.fixture
def with_audio(tmp_db, tmp_path, monkeypatch):
    """Give the seeded episodes real files on disk, inside a real media dir."""
    import config
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(config.settings, "media_dir", media)

    files = {}
    conn = sqlite3.connect(tmp_db)
    for i, episode_id in enumerate((EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3), start=1):
        path = media / f"{episode_id}.mp3"
        path.write_bytes(b"x" * (1000 * i))
        files[episode_id] = path
        conn.execute(
            "UPDATE episodes SET downloaded = 1, local_path = ?, created_at = ? WHERE id = ?",
            (str(path), _iso(90), episode_id),
        )
    conn.commit()
    conn.close()
    return {"media": media, "files": files}


class TestUsage:

    async def test_reports_what_is_on_disk(self, client, with_audio):
        r = await client.get("/storage")
        assert r.status_code == 200
        data = r.json()
        assert data["audio_bytes"] == 1000 + 2000 + 3000
        assert data["episodes"] == 3
        assert data["by_podcast"][0]["title"] == "Test Podcast"

    async def test_notices_files_no_episode_claims(self, client, with_audio):
        """A renamed id, an abandoned download, an unsubscribe."""
        stray = with_audio["media"] / "stray.mp3"
        stray.write_bytes(b"y" * 500)
        _stale(stray)
        data = (await client.get("/storage")).json()
        assert data["orphan_files"] == 1 and data["orphan_bytes"] == 500

    async def test_a_download_in_flight_is_not_a_stray(self, client, with_audio):
        """`downloader.py` writes `<name>.part` and renames on success, so a
        file being written is not something to reclaim — and the nightly job
        runs while the app is still serving."""
        part = with_audio["media"] / "ep-999.mp3.part"
        part.write_bytes(b"z" * 900)
        data = (await client.get("/storage")).json()
        assert data["orphan_files"] == 0

    async def test_a_file_deleted_by_hand_is_not_still_counted(self, client, with_audio):
        with_audio["files"][EPISODE_ID_1].unlink()
        data = (await client.get("/storage")).json()
        assert data["audio_bytes"] == 2000 + 3000

    async def test_retention_is_off_by_default(self, client):
        assert (await client.get("/storage")).json()["policy"]["days"] == 0


class TestPolicy:

    async def test_set_and_read_back(self, client):
        r = await client.put("/storage/policy", json={"days": 30, "played_only": False})
        assert r.json() == {"days": 30, "played_only": False}
        assert (await client.get("/storage")).json()["policy"]["days"] == 30

    async def test_negative_days_rejected(self, client):
        assert (await client.put("/storage/policy", json={"days": -1})).status_code == 422


class TestPrune:

    async def test_does_nothing_while_disabled(self, client, with_audio):
        """Retention has to be asked for. Deleting media unprompted is not a
        surprise this app should hold."""
        r = await client.post("/storage/prune")
        assert r.json()["status"] == "disabled"
        assert r.json()["freed_bytes"] == 0
        assert with_audio["files"][EPISODE_ID_1].exists()

    async def test_clears_played_audio_past_the_cutoff(self, client, with_audio):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        r = await client.post("/storage/prune?days=30")
        assert r.json()["episodes"] == 1
        assert r.json()["freed_bytes"] == 1000
        assert not with_audio["files"][EPISODE_ID_1].exists()
        assert with_audio["files"][EPISODE_ID_2].exists()

    async def test_the_transcript_survives(self, client, with_audio, tmp_db):
        """Audio can be downloaded again; a transcript is the part that cannot,
        and it is a thousandth of the size."""
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO transcripts (episode_id, words_json, created_at) "
            "VALUES (?, '[]', '2026-01-01T00:00:00')", (EPISODE_ID_1,))
        conn.commit()
        conn.close()
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        await client.post("/storage/prune?days=30")
        conn = sqlite3.connect(tmp_db)
        assert conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE episode_id = ?", (EPISODE_ID_1,)
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT downloaded, local_path FROM episodes WHERE id = ?", (EPISODE_ID_1,)
        ).fetchone()
        conn.close()
        assert row == (0, None)

    async def test_never_clears_something_queued(self, client, with_audio):
        """You put it there to listen to it."""
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        await client.post(f"/queue/{EPISODE_ID_1}")
        r = await client.post("/storage/prune?days=30")
        assert r.json()["episodes"] == 0
        assert with_audio["files"][EPISODE_ID_1].exists()

    async def test_never_clears_something_half_heard(self, client, with_audio, tmp_db):
        """A position with no finish is a bookmark of intent."""
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 300.0})
        r = await client.post("/storage/prune?days=30&")
        assert r.json()["episodes"] == 0
        assert with_audio["files"][EPISODE_ID_1].exists()

    async def test_unplayed_audio_is_kept_by_default(self, client, with_audio):
        r = await client.post("/storage/prune?days=30")
        assert r.json()["episodes"] == 0

    async def test_recent_audio_is_kept(self, client, with_audio, tmp_db):
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE episodes SET created_at = ? WHERE id = ?",
                     (_iso(1), EPISODE_ID_1))
        conn.commit()
        conn.close()
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        assert (await client.post("/storage/prune?days=30")).json()["episodes"] == 0

    async def test_dry_run_reports_without_deleting(self, client, with_audio):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        r = await client.post("/storage/prune?days=30&dry_run=true")
        assert r.json()["status"] == "dry_run"
        assert r.json()["freed_bytes"] == 1000
        assert with_audio["files"][EPISODE_ID_1].exists()

    async def test_orphans_are_swept_up(self, client, with_audio):
        stray = with_audio["media"] / "stray.mp3"
        stray.write_bytes(b"y" * 700)
        _stale(stray)
        r = await client.post("/storage/prune?days=30")
        assert r.json()["orphans"] == 1
        assert not stray.exists()

    async def test_never_deletes_a_download_in_flight(self, client, with_audio):
        part = with_audio["media"] / "ep-999.mp3.part"
        part.write_bytes(b"z" * 900)
        r = await client.post("/storage/prune?days=30")
        assert r.json()["orphans"] == 0
        assert part.exists(), "a download in progress was deleted"

    async def test_never_deletes_a_file_written_moments_ago(self, client, with_audio):
        """Covers the other half of the race: the rename has happened but the
        row pointing at it has not been written yet."""
        fresh = with_audio["media"] / "just-arrived.mp3"
        fresh.write_bytes(b"z" * 400)
        r = await client.post("/storage/prune?days=30")
        assert r.json()["orphans"] == 0
        assert fresh.exists()

    async def test_running_twice_is_a_no_op_the_second_time(self, client, with_audio):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        await client.post("/storage/prune?days=30")
        again = await client.post("/storage/prune?days=30")
        assert again.json()["episodes"] == 0 and again.json()["freed_bytes"] == 0
