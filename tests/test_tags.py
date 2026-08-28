"""Tags on subscriptions, and the feed filters that use them."""
import pytest


class TestTagCrud:

    @pytest.mark.asyncio
    async def test_create_and_list(self, client):
        r = await client.post("/tags", json={"name": "tech"})
        assert r.status_code == 200
        tag = r.json()
        assert tag["name"] == "tech" and tag["id"]

        listed = (await client.get("/tags")).json()
        assert [t["name"] for t in listed] == ["tech"]

    @pytest.mark.asyncio
    async def test_create_is_idempotent_case_insensitively(self, client):
        """The UI creates-or-picks in one call, so 'Tech' must not become a
        second tag alongside 'tech'."""
        a = (await client.post("/tags", json={"name": "tech"})).json()
        b = (await client.post("/tags", json={"name": "TECH"})).json()
        assert a["id"] == b["id"]
        assert len((await client.get("/tags")).json()) == 1

    @pytest.mark.asyncio
    async def test_whitespace_collapsed(self, client):
        t = (await client.post("/tags", json={"name": "  long   form  "})).json()
        assert t["name"] == "long form"

    @pytest.mark.asyncio
    async def test_empty_name_rejected(self, client):
        assert (await client.post("/tags", json={"name": "   "})).status_code == 422

    @pytest.mark.asyncio
    async def test_overlong_name_rejected(self, client):
        assert (await client.post("/tags", json={"name": "x" * 33})).status_code == 422

    @pytest.mark.asyncio
    async def test_sorted_case_insensitively(self, client):
        for n in ("Zeta", "alpha", "Beta"):
            await client.post("/tags", json={"name": n})
        assert [t["name"] for t in (await client.get("/tags")).json()] == ["alpha", "Beta", "Zeta"]


class TestTagAssignment:

    @pytest.mark.asyncio
    async def test_assign_and_read_back(self, client, seeded_podcast_id):
        tag = (await client.post("/tags", json={"name": "tech"})).json()
        r = await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [tag["id"]]})
        assert r.status_code == 200
        assert [t["name"] for t in r.json()] == ["tech"]

        subs = (await client.get("/podcasts/subscriptions")).json()
        sub = next(s for s in subs if s["podcast_id"] == seeded_podcast_id)
        assert [t["name"] for t in sub["tags"]] == ["tech"]

    @pytest.mark.asyncio
    async def test_assignment_replaces_wholesale(self, client, seeded_podcast_id):
        a = (await client.post("/tags", json={"name": "a"})).json()
        b = (await client.post("/tags", json={"name": "b"})).json()
        await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [a["id"], b["id"]]})
        out = (await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [b["id"]]})).json()
        assert [t["name"] for t in out] == ["b"]

    @pytest.mark.asyncio
    async def test_clearing_tags(self, client, seeded_podcast_id):
        a = (await client.post("/tags", json={"name": "a"})).json()
        await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [a["id"]]})
        assert (await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": []})).json() == []

    @pytest.mark.asyncio
    async def test_unknown_tag_id_ignored_not_half_applied(self, client, seeded_podcast_id):
        a = (await client.post("/tags", json={"name": "a"})).json()
        out = (await client.put(f"/tags/podcast/{seeded_podcast_id}",
                                json={"tag_ids": [a["id"], "does-not-exist"]})).json()
        assert [t["name"] for t in out] == ["a"]

    @pytest.mark.asyncio
    async def test_unknown_podcast_is_404(self, client):
        a = (await client.post("/tags", json={"name": "a"})).json()
        r = await client.put("/tags/podcast/nope", json={"tag_ids": [a["id"]]})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_podcast_count(self, client, seeded_podcast_id):
        a = (await client.post("/tags", json={"name": "a"})).json()
        assert (await client.get("/tags")).json()[0]["podcast_count"] == 0
        await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [a["id"]]})
        assert (await client.get("/tags")).json()[0]["podcast_count"] == 1

    @pytest.mark.asyncio
    async def test_delete_detaches_from_podcasts(self, client, seeded_podcast_id):
        a = (await client.post("/tags", json={"name": "a"})).json()
        await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [a["id"]]})
        await client.delete(f"/tags/{a['id']}")
        assert (await client.get("/tags")).json() == []
        subs = (await client.get("/podcasts/subscriptions")).json()
        sub = next(s for s in subs if s["podcast_id"] == seeded_podcast_id)
        assert sub["tags"] == []


class TestFeedFilters:

    @pytest.mark.asyncio
    async def test_unfiltered_returns_all(self, client):
        assert len((await client.get("/podcasts/feed")).json()) >= 1

    @pytest.mark.asyncio
    async def test_filter_by_tag(self, client, seeded_podcast_id):
        tag = (await client.post("/tags", json={"name": "tech"})).json()
        # Nothing tagged yet
        assert (await client.get(f"/podcasts/feed?tag_id={tag['id']}")).json() == []
        await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [tag["id"]]})
        out = (await client.get(f"/podcasts/feed?tag_id={tag['id']}")).json()
        assert out and all(e["podcast_id"] == seeded_podcast_id for e in out)

    @pytest.mark.asyncio
    async def test_search_matches_episode_title(self, client):
        out = (await client.get("/podcasts/feed?q=Episode One")).json()
        assert out and all("Episode One" in e["title"] for e in out)

    @pytest.mark.asyncio
    async def test_search_matches_podcast_title(self, client):
        out = (await client.get("/podcasts/feed?q=Test Podcast")).json()
        assert out and all(e["podcast_title"] == "Test Podcast" for e in out)

    @pytest.mark.asyncio
    async def test_search_no_match_is_empty_not_everything(self, client):
        assert (await client.get("/podcasts/feed?q=zzzznope")).json() == []

    @pytest.mark.asyncio
    async def test_filter_by_podcast_id(self, client, seeded_podcast_id):
        out = (await client.get(f"/podcasts/feed?podcast_id={seeded_podcast_id}")).json()
        assert out and all(e["podcast_id"] == seeded_podcast_id for e in out)

    @pytest.mark.asyncio
    async def test_status_transcribed(self, client):
        out = (await client.get("/podcasts/feed?status=transcribed")).json()
        assert all(e["transcript_status"] == "done" for e in out)

    @pytest.mark.asyncio
    async def test_unknown_status_ignored(self, client):
        """A typo must not silently return an empty library."""
        allx = (await client.get("/podcasts/feed")).json()
        out = (await client.get("/podcasts/feed?status=bogus")).json()
        assert len(out) == len(allx)

    @pytest.mark.asyncio
    async def test_filters_combine(self, client, seeded_podcast_id):
        tag = (await client.post("/tags", json={"name": "tech"})).json()
        await client.put(f"/tags/podcast/{seeded_podcast_id}", json={"tag_ids": [tag["id"]]})
        out = (await client.get(f"/podcasts/feed?tag_id={tag['id']}&q=Episode One")).json()
        assert out and all("Episode One" in e["title"] for e in out)

    @pytest.mark.asyncio
    async def test_limit_is_clamped(self, client):
        """An unbounded limit would let a caller pull the whole table."""
        r = await client.get("/podcasts/feed?limit=100000")
        assert r.status_code == 200
        assert len(r.json()) <= 200
