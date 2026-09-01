"""Checking a distilled claim against the world outside the podcast.

Every test here comes from one production report. `TAVILY_API_KEY` was unset, so
every search returned an empty list; nothing distinguished "no key" from "no
results"; the model was asked to analyse sources it had not been given and
correctly wrote four sections saying so; and the pipeline then marked the
research `done` and announced it on Telegram as ready.

The premise was thin as well: the distillation was five seconds from the very
start of an episode — "something happened that got brushed over in the news" —
which names nothing anyone could search for.
"""
import json
import sqlite3

import pytest

from conftest import EPISODE_ID_1, GIST_ID, PODCAST_ID
from services import researcher

pytestmark = pytest.mark.asyncio


@pytest.fixture
def with_key(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "tavily_api_key", "test-key")
    return settings


@pytest.fixture
def episode_context(tmp_db, monkeypatch):
    """A distillation with an episode, a summary and a transcript behind it."""
    import asyncio

    import database
    monkeypatch.setattr(database, "DB_PATH", tmp_db)
    # `summary` and friends arrive by migration, not in SCHEMA.
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(database.init_db())

    conn = sqlite3.connect(tmp_db)
    words, t = [], 0.0
    text = ("About a month ago something happened that got brushed over in the news. "
            "The container escape in the Kubernetes runtime, the one nobody covered. "
            "It matters because every managed cluster shipped that runtime by default.")
    for w in text.split():
        words.append({"word": " " + w, "start": round(t, 2), "end": round(t + 0.4, 2)})
        t += 0.5
    conn.execute("UPDATE episodes SET summary = ?, title = ? WHERE id = ?",
                 ("An episode about container security incidents.",
                  "AI is escaping containment", EPISODE_ID_1))
    conn.execute(
        "INSERT OR REPLACE INTO transcripts (episode_id, words_json, language, created_at) "
        "VALUES (?, ?, 'en', '2026-02-01T00:00:00')",
        (EPISODE_ID_1, json.dumps(words)))
    conn.execute("UPDATE gists SET start_seconds = 0, end_seconds = 5, text = ?, summary = ? "
                 "WHERE id = ?",
                 ("About a month ago something happened that got brushed over in the news",
                  json.dumps({"quote": "something happened that got brushed over",
                              "insight": "The excerpt ends before revealing the event."}),
                  GIST_ID))
    conn.commit()
    conn.close()
    return tmp_db


SOURCES = [
    {"url": "https://example.com/a", "title": "Runtime escape disclosed",
     "content": "A container escape was disclosed and patched in March.",
     "published": "2026-03-02", "query": "container escape runtime"},
    {"url": "https://example.com/b", "title": "Vendors dispute severity",
     "content": "Two vendors said the impact was overstated.",
     "published": "", "query": "container escape severity"},
]


class TestAvailability:

    def test_no_key_means_not_available(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "tavily_api_key", "")
        assert researcher.available() is False

    def test_a_key_is_all_it_takes(self, with_key):
        assert researcher.available() is True

    async def test_running_without_a_key_fails_with_a_reason(self, tmp_db, monkeypatch):
        """It used to write a report explaining that it had no sources."""
        import database
        from config import settings
        monkeypatch.setattr(settings, "tavily_api_key", "")
        monkeypatch.setattr(database, "DB_PATH", tmp_db)
        seed(tmp_db, "res-1")
        result = await researcher.run_research("res-1", GIST_ID)
        assert result["status"] == "error"
        assert "TAVILY_API_KEY" in result["error"]
        assert status_of(tmp_db, "res-1") == "error"

    async def test_the_endpoint_refuses_rather_than_queueing(self, client, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "tavily_api_key", "")
        r = await client.post(f"/research/{GIST_ID}")
        assert r.status_code == 409
        assert "Tavily" in r.json()["detail"]

    async def test_an_unknown_distillation_is_still_a_404(self, client, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "tavily_api_key", "")
        assert (await client.post("/research/nope")).status_code == 404


def seed(db_path, research_id, status="pending"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO researches (id, gist_id, episode_id, status, created_at) "
        "VALUES (?, ?, ?, ?, '2026-08-31T00:00:00')",
        (research_id, GIST_ID, EPISODE_ID_1, status))
    conn.commit()
    conn.close()


def status_of(db_path, research_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status, error FROM researches WHERE id = ?",
                           (research_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class TestPremise:
    """A five-second quote is not something anyone can research."""

    def test_carries_the_episode_and_the_speech_around_the_moment(self):
        premise = researcher.build_premise(
            {"text": "something happened", "start_seconds": 5,
             "summary": json.dumps({"quote": "something happened",
                                    "insight": "ends before revealing it"})},
            {"title": "AI is escaping containment", "podcast_title": "Show",
             "summary": "An episode about container security."},
            "the container escape in the Kubernetes runtime nobody covered",
        )
        assert "AI is escaping containment" in premise
        assert "container security" in premise
        assert "Kubernetes runtime" in premise
        assert "ends before revealing it" in premise

    def test_survives_a_distillation_with_no_model_summary(self):
        premise = researcher.build_premise(
            {"text": "a quote", "start_seconds": 0, "summary": None},
            {"title": "Ep"}, "")
        assert "a quote" in premise


class TestFailurePaths:

    async def test_no_sources_writes_no_report(self, episode_context, with_key, monkeypatch):
        """The bug in one line: a report from nothing was written and announced."""
        seed(episode_context, "res-2")

        async def plan(premise):
            return "Was there an unreported container escape?", ["container escape"]

        async def nothing(queries):
            return []
        monkeypatch.setattr(researcher, "plan", plan)
        monkeypatch.setattr(researcher, "search_web", nothing)
        wrote = []
        monkeypatch.setattr(researcher, "build_html",
                            lambda **kw: wrote.append(kw) or "<html>")
        told = []

        async def notify(text):
            told.append(text)
        monkeypatch.setattr(researcher, "notify", notify)

        result = await researcher.run_research("res-2", GIST_ID)
        assert result["status"] == "error"
        assert "No web sources" in result["error"]
        assert wrote == [], "wrote a report with no sources"
        assert told and "failed" in told[0].lower()

    async def test_no_queries_stops_early(self, episode_context, with_key, monkeypatch):
        async def plan(premise):
            return "", []
        monkeypatch.setattr(researcher, "plan", plan)
        seed(episode_context, "res-3")
        result = await researcher.run_research("res-3", GIST_ID)
        assert result["status"] == "error"
        assert status_of(episode_context, "res-3") == "error"

    async def test_a_failed_synthesis_is_not_a_report(self, episode_context, with_key, monkeypatch):
        async def plan(premise):
            return "claim", ["query"]

        async def sources(queries):
            return SOURCES

        async def no_report(claim, premise, srcs):
            return None
        monkeypatch.setattr(researcher, "plan", plan)
        monkeypatch.setattr(researcher, "search_web", sources)
        monkeypatch.setattr(researcher, "synthesise", no_report)
        seed(episode_context, "res-4")
        result = await researcher.run_research("res-4", GIST_ID)
        assert result["status"] == "error"

    async def test_a_failed_research_can_be_retried(self, client, tmp_db, with_key, monkeypatch):
        """The usual causes — a missing key, an empty search — both change."""
        seed(tmp_db, "res-old", status="error")

        async def never(research_id, gist_id):
            return {}
        monkeypatch.setattr("services.researcher.run_research", never)
        r = await client.post(f"/research/{GIST_ID}")
        assert r.status_code == 200
        assert r.json()["status"] == "pending"


class TestReport:

    @pytest.fixture
    def report(self):
        return {
            "verdict": "contested",
            "verdict_note": "One vendor confirmed it [1]; two dispute the severity [2].",
            "sections": [{"heading": "What happened", "body": "It was patched in March [1]."}],
            "open_questions": ["How many clusters shipped the runtime?"],
        }

    def test_sources_are_numbered_and_linked(self, report):
        html = researcher.build_html(
            claim="Was there an unreported container escape?", report=report,
            sources=SOURCES, echoes=[], episode={"title": "Ep", "podcast_title": "Show"},
            gist={"text": "something happened"}, queries=["container escape"])
        assert 'id="source-1"' in html and 'id="source-2"' in html
        assert 'href="https://example.com/a"' in html
        # Inline citations become links to those sources.
        assert 'href="#source-1"' in html
        assert "example.com" in html          # domain shown, not just the title

    def test_the_verdict_is_stated_not_buried(self, report):
        html = researcher.build_html(
            claim="c", report=report, sources=SOURCES, echoes=[],
            episode={"title": "Ep"}, gist={}, queries=[])
        assert "Contested" in html

    def test_what_was_searched_is_shown(self, report):
        """When an answer is thin, the searches say whether the question or the
        library was the problem."""
        html = researcher.build_html(
            claim="c", report=report, sources=SOURCES, echoes=[],
            episode={"title": "Ep"}, gist={}, queries=["container escape 2026"])
        assert "container escape 2026" in html

    def test_open_questions_are_kept(self, report):
        html = researcher.build_html(
            claim="c", report=report, sources=SOURCES, echoes=[],
            episode={"title": "Ep"}, gist={}, queries=[])
        assert "How many clusters" in html

    def test_the_library_cross_reference_deep_links(self, report):
        html = researcher.build_html(
            claim="c", report=report, sources=SOURCES,
            echoes=[{"podcast_title": "Other Show", "episode_title": "Related",
                     "start": 3725.0, "text": "they discussed the same runtime",
                     "episode_id": "ep-x"}],
            episode={"title": "Ep"}, gist={}, queries=[])
        assert "Elsewhere in your library" in html
        assert "Other Show" in html and "1:02:05" in html

    def test_a_report_with_no_library_echoes_omits_the_section(self, report):
        html = researcher.build_html(
            claim="c", report=report, sources=SOURCES, echoes=[],
            episode={"title": "Ep"}, gist={}, queries=[])
        assert "Elsewhere in your library" not in html


class TestHappyPath:

    async def test_end_to_end_with_stubs(self, episode_context, with_key, monkeypatch, tmp_path):
        from config import settings
        monkeypatch.setattr(settings, "reports_dir", tmp_path)

        async def plan(premise):
            assert "Kubernetes runtime" in premise, "the premise lost its context"
            return "Was there an unreported container escape?", ["container escape runtime"]

        async def sources(queries):
            return SOURCES

        async def synth(claim, premise, srcs):
            assert len(srcs) == 2
            return {"verdict": "contested",
                    "verdict_note": "Confirmed [1], disputed [2].",
                    "sections": [{"heading": "What happened", "body": "Patched [1]."}],
                    "open_questions": []}

        async def echoes(episode_id, claim, premise):
            return []

        async def notify(text):
            notify.said = text
        notify.said = ""

        monkeypatch.setattr(researcher, "plan", plan)
        monkeypatch.setattr(researcher, "search_web", sources)
        monkeypatch.setattr(researcher, "synthesise", synth)
        monkeypatch.setattr(researcher, "library_echoes", echoes)
        monkeypatch.setattr(researcher, "notify", notify)

        seed(episode_context, "res-5")
        result = await researcher.run_research("res-5", GIST_ID)
        assert result["status"] == "done"
        assert result["verdict"] == "contested"
        assert status_of(episode_context, "res-5") == "done"

        written = (tmp_path / "res-5.html").read_text()
        assert "Contested" in written and "example.com/a" in written
        assert "Contested" in notify.said


class TestSourceQuality:
    """From the first real report: five of thirteen sources were variations of
    the same npm documentation page, because the model wrote `site:`-scoped
    queries."""

    async def test_one_domain_cannot_fill_the_source_list(self, with_key, monkeypatch):
        pages = [
            {"url": f"https://docs.npmjs.com/page-{i}", "title": f"npm docs {i}",
             "content": "scripts"} for i in range(6)
        ]
        other = [{"url": "https://blog.rust-lang.org/advisory",
                  "title": "Advisory", "content": "what happened"}]

        class FakeResponse:
            def __init__(self, results):
                self._results = results

            def raise_for_status(self):
                pass

            def json(self):
                return {"results": self._results}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None):
                return FakeResponse(pages if "npm" in json["query"] else other)

        monkeypatch.setattr(researcher.httpx, "AsyncClient", lambda **kw: FakeClient())
        sources = await researcher.search_web(["npm postinstall", "rust advisory"])
        from collections import Counter
        by_domain = Counter(s["url"].split("/")[2] for s in sources)
        assert by_domain["docs.npmjs.com"] <= researcher.MAX_PER_DOMAIN
        assert "blog.rust-lang.org" in by_domain, "the cap starved the other domains"

    async def test_a_search_that_fails_does_not_lose_the_others(self, with_key, monkeypatch):
        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None):
                if json["query"] == "boom":
                    raise RuntimeError("network")

                class R:
                    def raise_for_status(self): pass
                    def json(self): return {"results": [
                        {"url": "https://ok.example/1", "title": "Fine", "content": "x"}]}
                return R()

        monkeypatch.setattr(researcher.httpx, "AsyncClient", lambda **kw: FakeClient())
        sources = await researcher.search_web(["boom", "fine"])
        assert [s["url"] for s in sources] == ["https://ok.example/1"]

    def test_a_long_transcript_quote_is_trimmed_not_dumped(self):
        long_quote = " ".join(["word"] * 300)
        html = researcher.build_html(
            claim="c",
            report={"verdict": "mixed", "verdict_note": "n", "sections": [],
                    "open_questions": []},
            sources=SOURCES, echoes=[], episode={"title": "Ep"},
            gist={"text": long_quote}, queries=[])
        assert "…" in html
        assert html.count("word") < 200


class TestMarkdownExport:
    """A finished report used to exist only as a page to look at, so anything
    worth keeping had to be selected out of a browser tab."""

    REPORT = {
        "verdict": "mixed",
        "verdict_note": "Confirmed in part [1]; disputed [2].",
        "sections": [{"heading": "What happened", "body": "A tool found three CVEs [1]."}],
        "open_questions": ["How many were exploitable?"],
    }

    def test_it_carries_the_verdict_the_claim_and_the_sources(self):
        md = researcher.build_markdown(
            claim="Do agents outpace patching?", report=self.REPORT, sources=SOURCES,
            echoes=[], episode={"title": "Ep", "podcast_title": "Show"},
            gist={"text": "a quote"}, queries=["agents patching"])
        assert md.startswith("# Do agents outpace patching?")
        assert "**Mixed evidence**" in md
        assert "> a quote" in md
        assert "## What happened" in md
        assert "1. [Runtime escape disclosed](https://example.com/a)" in md
        assert "Searched: agents patching" in md

    def test_open_questions_and_library_echoes_survive(self):
        md = researcher.build_markdown(
            claim="c", report=self.REPORT, sources=SOURCES,
            echoes=[{"podcast_title": "Low Level", "episode_title": "Rust worms",
                     "start": 3725.0}],
            episode={"title": "Ep"}, gist={}, queries=[])
        assert "How many were exploitable?" in md
        assert "Low Level" in md and "1:02:05" in md

    def test_a_long_quote_is_trimmed_here_too(self):
        md = researcher.build_markdown(
            claim="c", report=self.REPORT, sources=SOURCES, echoes=[],
            episode={"title": "Ep"}, gist={"text": " ".join(["word"] * 300)}, queries=[])
        assert "…" in md

    async def test_the_endpoint_renders_from_the_stored_structure(
        self, client, episode_context, with_key, monkeypatch, tmp_path,
    ):
        from config import settings
        monkeypatch.setattr(settings, "reports_dir", tmp_path)

        async def plan(premise):
            return "Was there an unreported container escape?", ["container escape"]

        async def sources(queries):
            return SOURCES

        async def synth(claim, premise, srcs):
            return self.REPORT

        async def echoes(episode_id, claim, queries):
            return []

        async def notify(text):
            return None

        for name, fn in (("plan", plan), ("search_web", sources), ("synthesise", synth),
                         ("library_echoes", echoes), ("notify", notify)):
            monkeypatch.setattr(researcher, name, fn)

        seed(episode_context, "res-md")
        assert (await researcher.run_research("res-md", GIST_ID))["status"] == "done"

        r = await client.get(f"/research/{GIST_ID}/markdown")
        assert r.status_code == 200
        assert "**Mixed evidence**" in r.json()["markdown"]
        assert "https://example.com/a" in r.json()["markdown"]

    async def test_a_report_from_before_this_says_so(self, client, tmp_db, with_key):
        """Rather than half-recovering something from the page."""
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO researches (id, gist_id, episode_id, status, public_url, created_at) "
            "VALUES ('old', ?, ?, 'done', 'https://x/y.html', '2026-01-01T00:00:00')",
            (GIST_ID, EPISODE_ID_1))
        conn.commit()
        conn.close()
        r = await client.get(f"/research/{GIST_ID}/markdown")
        assert r.status_code == 409
        assert "run it again" in r.json()["detail"]

    async def test_no_report_no_markdown(self, client, with_key):
        assert (await client.get(f"/research/{GIST_ID}/markdown")).status_code == 404


class TestPdfEndpoint:

    async def test_no_binary_no_pdf(self, client, tmp_db, with_key, monkeypatch):
        """A button that can only fail should not be offered, and the endpoint
        behind it says why rather than returning a blank page."""
        from services import typeset
        monkeypatch.setattr(typeset, "available", lambda: False)
        seed(tmp_db, "res-pdf", status="done")
        r = await client.get(f"/research/{GIST_ID}/pdf")
        assert r.status_code == 409
        assert "typst" in r.json()["detail"]

    async def test_a_report_without_stored_structure_cannot_be_typeset(
        self, client, tmp_db, with_key, monkeypatch,
    ):
        from services import typeset
        monkeypatch.setattr(typeset, "available", lambda: True)
        seed(tmp_db, "res-old-pdf", status="done")
        r = await client.get(f"/research/{GIST_ID}/pdf")
        assert r.status_code == 409
        assert "run it again" in r.json()["detail"]

    async def test_the_status_says_which_renderings_are_possible(
        self, client, tmp_db, with_key, monkeypatch,
    ):
        from services import typeset
        monkeypatch.setattr(typeset, "available", lambda: True)
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO researches (id, gist_id, episode_id, status, public_url, "
            "                        report_json, created_at) "
            "VALUES ('r1', ?, ?, 'done', 'https://x/y.html', ?, '2026-01-01T00:00:00')",
            (GIST_ID, EPISODE_ID_1, json.dumps({"claim": "c"})))
        conn.commit()
        conn.close()
        body = (await client.get(f"/research/{GIST_ID}")).json()
        assert body["markdown"] is True and body["pdf"] is True
        # …and the raw structure is not shipped to the client.
        assert "report_json" not in body

    async def test_a_finished_report_is_typeset_and_cached(
        self, client, tmp_db, with_key, monkeypatch, tmp_path,
    ):
        from config import settings
        from services import typeset
        monkeypatch.setattr(settings, "reports_dir", tmp_path)

        calls = []

        def fake_render(data, out_path):
            calls.append(data)
            out_path.write_bytes(b"%PDF-1.7 fake")
            return out_path

        monkeypatch.setattr(typeset, "available", lambda: True)
        monkeypatch.setattr(typeset, "render", fake_render)

        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO researches (id, gist_id, episode_id, status, report_json, created_at) "
            "VALUES ('r2', ?, ?, 'done', ?, '2026-01-01T00:00:00')",
            (GIST_ID, EPISODE_ID_1, json.dumps({"claim": "c", "report": {}})))
        conn.commit()
        conn.close()

        first = await client.get(f"/research/{GIST_ID}/pdf")
        assert first.status_code == 200
        assert first.headers["content-type"] == "application/pdf"
        assert first.content.startswith(b"%PDF")

        second = await client.get(f"/research/{GIST_ID}/pdf")
        assert second.status_code == 200
        assert len(calls) == 1, "typeset the same report twice"
