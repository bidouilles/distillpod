"""Up Next, server-side.

The queue used to live only in the browser that built it, which made it the one
piece of listening state that did not survive picking up the phone. These tests
are mostly about that contract: what the server returns is the queue, in order,
and every mutation returns the whole new order so a client never has to guess.
"""
import pytest

from conftest import EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3

pytestmark = pytest.mark.asyncio


async def ids(client) -> list[str]:
    r = await client.get("/queue")
    assert r.status_code == 200
    return [i["episode_id"] for i in r.json()]


class TestEnqueue:

    async def test_starts_empty(self, client):
        assert await ids(client) == []

    async def test_append_keeps_arrival_order(self, client):
        await client.post(f"/queue/{EPISODE_ID_1}")
        await client.post(f"/queue/{EPISODE_ID_2}")
        assert await ids(client) == [EPISODE_ID_1, EPISODE_ID_2]

    async def test_play_next_goes_to_the_front(self, client):
        await client.post(f"/queue/{EPISODE_ID_1}")
        await client.post(f"/queue/{EPISODE_ID_2}?position=next")
        assert await ids(client) == [EPISODE_ID_2, EPISODE_ID_1]

    async def test_re_adding_moves_rather_than_duplicates(self, client):
        """A queue holding the same episode twice is never what was meant."""
        await client.post(f"/queue/{EPISODE_ID_1}")
        await client.post(f"/queue/{EPISODE_ID_2}")
        await client.post(f"/queue/{EPISODE_ID_1}?position=next")
        assert await ids(client) == [EPISODE_ID_1, EPISODE_ID_2]

    async def test_unknown_episode_rejected(self, client):
        assert (await client.post("/queue/nope")).status_code == 404

    async def test_rejects_an_unrecognised_position(self, client):
        assert (await client.post(f"/queue/{EPISODE_ID_1}?position=middle")).status_code == 422

    async def test_row_carries_enough_to_play_it(self, client):
        """The queue screen plays a row directly, so it cannot need a second
        request per item to find the audio or the artwork."""
        await client.post(f"/queue/{EPISODE_ID_1}")
        item = (await client.get("/queue")).json()[0]
        assert item["audio_url"] and item["title"]
        assert item["podcast_title"] == "Test Podcast"
        assert item["image_url"] == "https://example.com/img.jpg"
        assert item["duration_seconds"] == 3600


class TestReorder:

    async def test_replace_sets_the_order(self, client):
        for ep in (EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3):
            await client.post(f"/queue/{ep}")
        r = await client.put("/queue", json={"episode_ids": [EPISODE_ID_3, EPISODE_ID_1]})
        assert r.status_code == 200
        assert [i["episode_id"] for i in r.json()] == [EPISODE_ID_3, EPISODE_ID_1]
        assert await ids(client) == [EPISODE_ID_3, EPISODE_ID_1]

    async def test_replace_drops_unknown_ids_rather_than_failing(self, client):
        """A client may be flushing a mirror written before an unsubscribe.
        Rejecting the whole write would leave the two copies permanently apart."""
        r = await client.put("/queue", json={"episode_ids": ["gone", EPISODE_ID_2]})
        assert r.status_code == 200
        assert [i["episode_id"] for i in r.json()] == [EPISODE_ID_2]

    async def test_replace_deduplicates(self, client):
        r = await client.put("/queue", json={"episode_ids": [EPISODE_ID_1, EPISODE_ID_1]})
        assert [i["episode_id"] for i in r.json()] == [EPISODE_ID_1]

    async def test_replace_with_nothing_clears(self, client):
        await client.post(f"/queue/{EPISODE_ID_1}")
        assert (await client.put("/queue", json={"episode_ids": []})).json() == []


class TestRemove:

    async def test_remove_one(self, client):
        await client.post(f"/queue/{EPISODE_ID_1}")
        await client.post(f"/queue/{EPISODE_ID_2}")
        r = await client.delete(f"/queue/{EPISODE_ID_1}")
        assert [i["episode_id"] for i in r.json()] == [EPISODE_ID_2]

    async def test_removing_something_absent_is_not_an_error(self, client):
        assert (await client.delete(f"/queue/{EPISODE_ID_1}")).status_code == 200

    async def test_clear(self, client):
        await client.post(f"/queue/{EPISODE_ID_1}")
        assert (await client.delete("/queue")).json() == []
        assert await ids(client) == []


class TestFeedIntegration:

    async def test_feed_says_what_is_queued(self, client):
        """The feed has to be able to draw the queue badge without a second
        request per row."""
        await client.post(f"/queue/{EPISODE_ID_2}")
        feed = (await client.get("/podcasts/feed")).json()
        queued = {e["id"]: e["queued"] for e in feed}
        assert queued[EPISODE_ID_2] == 1
        assert queued[EPISODE_ID_1] == 0
