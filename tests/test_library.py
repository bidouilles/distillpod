"""Library organisation: the feed's new filters, the inbox, and OPML.

`unplayed` is the one worth reading the reasoning for. It used to be applied on
the client from this browser's localStorage, so an episode finished on the phone
still counted as unplayed on the laptop — the filter quietly lied. Playback
state has been server-side for a while; these tests pin it there.
"""
import sqlite3

import pytest

from conftest import EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3, PODCAST_ID

pytestmark = pytest.mark.asyncio


async def feed_ids(client, query: str = "") -> list[str]:
    r = await client.get(f"/podcasts/feed{query}")
    assert r.status_code == 200, r.text
    return [e["id"] for e in r.json()]


class TestUnplayedFilter:

    async def test_everything_is_unplayed_to_begin_with(self, client):
        assert len(await feed_ids(client, "?unplayed=true")) == 3

    async def test_reads_server_side_playback_state(self, client):
        """The point of the change: finished on another device still counts."""
        await client.put(f"/player/progress/{EPISODE_ID_2}", json={"played": True})
        ids = await feed_ids(client, "?unplayed=true")
        assert EPISODE_ID_2 not in ids and len(ids) == 2

    async def test_a_position_alone_does_not_count_as_played(self, client):
        """Half-heard is not heard — it should still show up as something to
        finish, which is exactly what Continue listening is for."""
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 120.0})
        assert EPISODE_ID_1 in await feed_ids(client, "?unplayed=true")

    async def test_feed_row_reports_played_and_position(self, client):
        await client.put(f"/player/progress/{EPISODE_ID_1}",
                         json={"position": 90.0, "played": True})
        ep = next(e for e in (await client.get("/podcasts/feed")).json()
                  if e["id"] == EPISODE_ID_1)
        assert ep["played"] == 1 and ep["position"] == 90.0


class TestDurationFilters:

    async def test_max_minutes(self, client):
        assert await feed_ids(client, "?max_minutes=40") == [EPISODE_ID_2]

    async def test_min_minutes(self, client):
        assert await feed_ids(client, "?min_minutes=45") == [EPISODE_ID_1]

    async def test_an_unknown_length_is_never_claimed_to_be_short(self, client):
        """Answering 'I have 20 minutes' with an episode nobody has measured is
        worse than answering with nothing."""
        assert EPISODE_ID_3 not in await feed_ids(client, "?max_minutes=40")

    async def test_a_range_combines(self, client):
        assert await feed_ids(client, "?min_minutes=20&max_minutes=40") == [EPISODE_ID_2]

    async def test_filters_combine_with_status(self, client):
        assert await feed_ids(client, "?max_minutes=40&status=transcribed") == [EPISODE_ID_2]

    async def test_absurd_bounds_rejected(self, client):
        assert (await client.get("/podcasts/feed?max_minutes=99999")).status_code == 422


class TestSort:

    async def test_newest_is_the_default(self, client):
        assert await feed_ids(client) == [EPISODE_ID_3, EPISODE_ID_2, EPISODE_ID_1]

    async def test_oldest(self, client):
        assert await feed_ids(client, "?sort=oldest") == [
            EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3]

    async def test_shortest_puts_unknown_lengths_last(self, client):
        assert await feed_ids(client, "?sort=shortest") == [
            EPISODE_ID_2, EPISODE_ID_1, EPISODE_ID_3]

    async def test_longest_also_puts_unknown_lengths_last(self, client):
        assert await feed_ids(client, "?sort=longest") == [
            EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3]

    async def test_an_unknown_sort_falls_back_rather_than_failing(self, client):
        assert await feed_ids(client, "?sort=sideways") == [
            EPISODE_ID_3, EPISODE_ID_2, EPISODE_ID_1]


class TestInbox:

    async def test_an_existing_library_is_not_one_giant_unread_pile(self, client):
        """Nobody wants to open the app to '2,431 new'."""
        assert (await client.get("/podcasts/inbox")).json()["new"] == 0

    async def test_marking_seen_sets_the_line(self, client):
        r = await client.post("/podcasts/inbox/seen")
        assert r.json()["new"] == 0 and r.json()["since"]

    async def test_counts_what_arrived_after_the_line(self, client, tmp_db):
        await client.post("/podcasts/inbox/seen")
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """INSERT INTO episodes (id, podcast_id, title, audio_url, published_at,
                                     created_at)
               VALUES ('ep_new', ?, 'Brand new', 'https://a/x.mp3',
                       '2026-02-04T00:00:00', '2099-01-01T00:00:00')""",
            (PODCAST_ID,),
        )
        conn.commit()
        conn.close()
        assert (await client.get("/podcasts/inbox")).json()["new"] == 1

    async def test_a_backfilled_old_upload_is_not_news(self, client, tmp_db):
        """A channel import brings in years of videos at once; none of them
        arrived because they were published today."""
        await client.post("/podcasts/inbox/seen")
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """INSERT INTO episodes (id, podcast_id, title, audio_url, published_at,
                                     created_at)
               VALUES ('ep_old', ?, 'From 2019', 'https://a/y.mp3',
                       '2019-01-01T00:00:00', '2019-01-02T00:00:00')""",
            (PODCAST_ID,),
        )
        conn.commit()
        conn.close()
        assert (await client.get("/podcasts/inbox")).json()["new"] == 0

    async def test_ingest_stamps_created_at_without_the_caller_saying_so(self, client, tmp_db):
        """Six insert sites feed this column, and a new one will be written
        without reading this file — so the database stamps it, not the caller."""
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """INSERT INTO episodes (id, podcast_id, title, audio_url)
               VALUES ('ep_stamp', ?, 'No date given', 'https://a/z.mp3')""",
            (PODCAST_ID,),
        )
        conn.commit()
        row = conn.execute("SELECT created_at FROM episodes WHERE id = 'ep_stamp'").fetchone()
        conn.close()
        assert row[0], "the trigger did not stamp created_at"


class TestOpml:

    async def test_export_is_a_download(self, client):
        r = await client.get("/podcasts/opml")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert "https://feeds.example.com/test" in r.text
        assert "Test Podcast" in r.text

    async def test_round_trip(self, client):
        exported = (await client.get("/podcasts/opml")).text
        r = await client.post("/podcasts/opml", json={"xml": exported})
        # Everything in it is already subscribed, so nothing is added twice.
        assert r.json() == {"added": 0, "skipped": 1, "found": 1, "titles": []}

    async def test_import_adds_new_feeds(self, client):
        xml = """<?xml version="1.0"?><opml version="2.0"><body>
          <outline text="Folder">
            <outline type="rss" text="Imported Show" xmlUrl="https://example.com/rss"/>
          </outline>
        </body></opml>"""
        r = await client.post("/podcasts/opml", json={"xml": xml})
        assert r.json()["added"] == 1
        subs = (await client.get("/podcasts/subscriptions")).json()
        assert "Imported Show" in [s["title"] for s in subs]

    async def test_importing_twice_changes_nothing(self, client):
        xml = ('<opml version="2.0"><body><outline type="rss" text="X" '
               'xmlUrl="https://example.com/rss"/></body></opml>')
        await client.post("/podcasts/opml", json={"xml": xml})
        again = await client.post("/podcasts/opml", json={"xml": xml})
        assert again.json() == {"added": 0, "skipped": 1, "found": 1, "titles": []}

    async def test_rubbish_is_rejected_with_a_reason(self, client):
        r = await client.post("/podcasts/opml", json={"xml": "not xml at all"})
        assert r.status_code == 422

    async def test_a_file_with_no_feeds_is_not_an_error(self, client):
        r = await client.post("/podcasts/opml", json={"xml": "<opml><body/></opml>"})
        assert r.json()["found"] == 0


class TestPodcastSettings:

    async def test_default_is_no_opinion(self, client):
        sub = (await client.get("/podcasts/subscriptions")).json()[0]
        assert sub["settings"] == {
            "playback_rate": None, "skip_intro": None, "skip_outro": None,
            "prefer_adfree": None, "auto_transcribe": None,
        }

    async def test_saved_and_read_back(self, client):
        r = await client.put(f"/podcasts/{PODCAST_ID}/settings", json={
            "playback_rate": 1.5, "skip_intro": 30, "prefer_adfree": True,
        })
        assert r.json()["playback_rate"] == 1.5
        sub = (await client.get("/podcasts/subscriptions")).json()[0]
        assert sub["settings"]["skip_intro"] == 30
        assert sub["settings"]["prefer_adfree"] is True

    async def test_a_field_can_be_cleared_back_to_no_opinion(self, client):
        await client.put(f"/podcasts/{PODCAST_ID}/settings", json={"playback_rate": 2.0})
        r = await client.put(f"/podcasts/{PODCAST_ID}/settings", json={})
        assert r.json()["playback_rate"] is None

    async def test_absurd_rate_rejected(self, client):
        r = await client.put(f"/podcasts/{PODCAST_ID}/settings", json={"playback_rate": 12})
        assert r.status_code == 422

    async def test_absurd_skip_rejected(self, client):
        r = await client.put(f"/podcasts/{PODCAST_ID}/settings", json={"skip_intro": 9000})
        assert r.status_code == 422

    async def test_unknown_podcast(self, client):
        assert (await client.put("/podcasts/nope/settings", json={})).status_code == 404

    async def test_play_returns_them_so_the_player_needs_no_second_request(self, client):
        await client.put(f"/podcasts/{PODCAST_ID}/settings", json={"playback_rate": 1.5})
        r = await client.post("/player/play", json={
            "episode_id": EPISODE_ID_2, "audio_url": "https://audio.example.com/2.mp3",
        })
        assert r.json()["settings"]["playback_rate"] == 1.5

    async def test_auto_transcribe_off_does_not_queue_transcription(self, client, tmp_db):
        """A load control, not a preference: transcription is the one stage that
        can cost money or pin a core for minutes."""
        await client.put(f"/podcasts/{PODCAST_ID}/settings", json={"auto_transcribe": False})
        r = await client.post("/player/play", json={
            "episode_id": EPISODE_ID_1, "audio_url": "https://audio.example.com/1.mp3",
        })
        assert r.status_code == 200
        conn = sqlite3.connect(tmp_db)
        status = conn.execute(
            "SELECT transcript_status FROM episodes WHERE id = ?", (EPISODE_ID_1,)
        ).fetchone()[0]
        conn.close()
        assert status == "none"
