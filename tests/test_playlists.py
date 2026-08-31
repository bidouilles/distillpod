"""Playlists, manual and smart.

The design claim under test is that a smart playlist means exactly what the
same filter chips mean on the feed, because both build their query in
`services/episode_query.py`. So most of these assert on selection, and a couple
assert the two kinds cannot be muddled — a smart playlist has no manual
membership, and a manual one has no rules.
"""
import pytest

from conftest import EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3, PODCAST_ID

pytestmark = pytest.mark.asyncio


async def make(client, name="Test", kind="manual", rules=None):
    body = {"name": name, "kind": kind}
    if rules is not None:
        body["rules"] = rules
    r = await client.post("/playlists", json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestCrud:

    async def test_starts_empty(self, client):
        assert (await client.get("/playlists")).json() == []

    async def test_create_and_list(self, client):
        await make(client, "Listen later")
        listed = (await client.get("/playlists")).json()
        assert [p["name"] for p in listed] == ["Listen later"]
        assert listed[0]["kind"] == "manual"
        assert listed[0]["episode_count"] == 0

    async def test_name_is_cleaned(self, client):
        p = await make(client, "  road   trip  ")
        assert p["name"] == "road trip"

    async def test_empty_name_rejected(self, client):
        assert (await client.post("/playlists", json={"name": "   "})).status_code == 422

    async def test_rename(self, client):
        p = await make(client, "Old")
        r = await client.patch(f"/playlists/{p['id']}", json={"name": "New"})
        assert r.json()["name"] == "New"

    async def test_delete_takes_its_members_with_it(self, client):
        p = await make(client)
        await client.post(f"/playlists/{p['id']}/episodes/{EPISODE_ID_1}")
        assert (await client.delete(f"/playlists/{p['id']}")).status_code == 200
        assert (await client.get("/playlists")).json() == []
        assert (await client.get(f"/playlists/{p['id']}")).status_code == 404


class TestManualMembership:

    async def test_add_and_read_back_in_order(self, client):
        p = await make(client)
        for ep in (EPISODE_ID_3, EPISODE_ID_1):
            await client.post(f"/playlists/{p['id']}/episodes/{ep}")
        got = (await client.get(f"/playlists/{p['id']}")).json()
        assert [e["id"] for e in got["episodes"]] == [EPISODE_ID_3, EPISODE_ID_1]
        assert got["playlist"]["episode_count"] == 2

    async def test_adding_twice_is_a_no_op(self, client):
        p = await make(client)
        await client.post(f"/playlists/{p['id']}/episodes/{EPISODE_ID_1}")
        await client.post(f"/playlists/{p['id']}/episodes/{EPISODE_ID_1}")
        got = (await client.get(f"/playlists/{p['id']}")).json()
        assert len(got["episodes"]) == 1

    async def test_unknown_episode_rejected(self, client):
        p = await make(client)
        assert (await client.post(f"/playlists/{p['id']}/episodes/nope")).status_code == 404

    async def test_remove(self, client):
        p = await make(client)
        await client.post(f"/playlists/{p['id']}/episodes/{EPISODE_ID_1}")
        await client.delete(f"/playlists/{p['id']}/episodes/{EPISODE_ID_1}")
        assert (await client.get(f"/playlists/{p['id']}")).json()["episodes"] == []

    async def test_reorder(self, client):
        p = await make(client)
        for ep in (EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3):
            await client.post(f"/playlists/{p['id']}/episodes/{ep}")
        r = await client.put(f"/playlists/{p['id']}/episodes",
                             json={"episode_ids": [EPISODE_ID_3, EPISODE_ID_2]})
        assert r.status_code == 200
        got = (await client.get(f"/playlists/{p['id']}")).json()["episodes"]
        # Episodes the client did not mention keep their place after the ones it did.
        assert [e["id"] for e in got][:2] == [EPISODE_ID_3, EPISODE_ID_2]
        assert got[2]["id"] == EPISODE_ID_1

    async def test_a_smart_playlist_has_no_manual_membership(self, client):
        p = await make(client, kind="smart", rules={"unplayed": True})
        r = await client.post(f"/playlists/{p['id']}/episodes/{EPISODE_ID_1}")
        assert r.status_code == 409


class TestSmartRules:

    async def test_duration_rule_selects_short_episodes(self, client):
        """'I have 20 minutes' is the most common real query."""
        p = await make(client, "Quick", kind="smart", rules={"max_minutes": 40})
        got = (await client.get(f"/playlists/{p['id']}")).json()["episodes"]
        # ep1 is 60min, ep2 is 30min, ep3's length is unknown.
        assert [e["id"] for e in got] == [EPISODE_ID_2]

    async def test_status_rule(self, client):
        p = await make(client, "Ready", kind="smart", rules={"status": "transcribed"})
        got = (await client.get(f"/playlists/{p['id']}")).json()["episodes"]
        assert [e["id"] for e in got] == [EPISODE_ID_2]

    async def test_sort_rule(self, client):
        p = await make(client, "Oldest", kind="smart", rules={"sort": "oldest"})
        got = (await client.get(f"/playlists/{p['id']}")).json()["episodes"]
        assert [e["id"] for e in got] == [EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3]

    async def test_limit_rule(self, client):
        p = await make(client, "Two", kind="smart", rules={"limit": 2})
        assert len((await client.get(f"/playlists/{p['id']}")).json()["episodes"]) == 2

    async def test_rules_can_be_edited(self, client):
        p = await make(client, "Quick", kind="smart", rules={"max_minutes": 40})
        r = await client.patch(f"/playlists/{p['id']}", json={"rules": {"max_minutes": 200}})
        assert r.status_code == 200
        got = (await client.get(f"/playlists/{p['id']}")).json()["episodes"]
        assert len(got) == 2                      # both episodes with a known length

    async def test_count_is_resolved_now_not_when_it_was_made(self, client):
        """The whole point of a smart playlist: 'Quick listen (7)' has to be true
        now. Playing ep2 must drop it out of an unplayed rule immediately."""
        p = await make(client, "Fresh", kind="smart", rules={"unplayed": True})
        before = (await client.get("/playlists")).json()[0]["episode_count"]
        await client.put(f"/player/progress/{EPISODE_ID_2}", json={"played": True})
        after = (await client.get("/playlists")).json()[0]["episode_count"]
        assert after == before - 1

    async def test_a_manual_playlist_has_no_rules(self, client):
        p = await make(client)
        r = await client.patch(f"/playlists/{p['id']}", json={"rules": {"unplayed": True}})
        assert r.status_code == 409

    async def test_tag_rule(self, client):
        tag = (await client.post("/tags", json={"name": "tech"})).json()
        p = await make(client, "Tech", kind="smart", rules={"tag_id": tag["id"]})
        assert (await client.get(f"/playlists/{p['id']}")).json()["episodes"] == []
        await client.put(f"/tags/podcast/{PODCAST_ID}", json={"tag_ids": [tag["id"]]})
        assert len((await client.get(f"/playlists/{p['id']}")).json()["episodes"]) == 3


class TestQueueing:

    async def test_play_all_appends_to_the_queue(self, client):
        p = await make(client)
        for ep in (EPISODE_ID_2, EPISODE_ID_1):
            await client.post(f"/playlists/{p['id']}/episodes/{ep}")
        r = await client.post(f"/playlists/{p['id']}/queue")
        assert [i["episode_id"] for i in r.json()] == [EPISODE_ID_2, EPISODE_ID_1]

    async def test_play_all_can_replace_the_queue(self, client):
        await client.post(f"/queue/{EPISODE_ID_3}")
        p = await make(client)
        await client.post(f"/playlists/{p['id']}/episodes/{EPISODE_ID_1}")
        r = await client.post(f"/playlists/{p['id']}/queue?replace=true")
        assert [i["episode_id"] for i in r.json()] == [EPISODE_ID_1]

    async def test_play_all_on_a_smart_playlist(self, client):
        p = await make(client, "Ready", kind="smart", rules={"status": "transcribed"})
        r = await client.post(f"/playlists/{p['id']}/queue")
        assert [i["episode_id"] for i in r.json()] == [EPISODE_ID_2]

    async def test_unknown_playlist(self, client):
        assert (await client.post("/playlists/nope/queue")).status_code == 404
