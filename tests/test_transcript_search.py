"""
Full-text transcript search.

The point of this feature is the timestamp: FTS5 can tell you an episode
mentions something, but a search that cannot tell you *when* it was said is
barely worth having, so most of these tests are about position.
"""
import json

import pytest

from services.transcript_search import find_matches, fts_query, normalise, query_terms


def words(text: str, step: float = 0.5, start: float = 0.0):
    """Build a words_json payload with one word every `step` seconds."""
    out, t = [], start
    for w in text.split():
        out.append({"word": " " + w, "start": round(t, 2), "end": round(t + step, 2)})
        t += step
    return json.dumps(out)


class TestNormalise:

    def test_folds_accents(self):
        """Nobody types accents into a search box."""
        assert normalise("rétro") == normalise("retro")
        assert normalise("Ingénierie") == "ingenierie"

    def test_strips_punctuation(self):
        assert normalise("engineering,") == "engineering"
        assert normalise("l'IA") == "lia"

    def test_query_terms_splits_and_drops_empties(self):
        assert query_terms("  reverse   Engineering ") == ["reverse", "engineering"]
        assert query_terms("   ") == []


class TestFtsQuery:

    def test_tokens_are_quoted(self):
        assert fts_query("reverse engineering") == '"reverse" "engineering"'

    def test_punctuation_cannot_become_fts_syntax(self):
        """Unquoted, these are FTS5 operators and raise instead of matching."""
        for raw in ("retro-ingenierie", "a OR b", "(", "x*", 'say "hi"'):
            out = fts_query(raw)
            assert out.startswith('"') and out.endswith('"')

    def test_embedded_quote_escaped(self):
        assert '""' in fts_query('he said "no"')

    def test_empty(self):
        assert fts_query("   ") == ""


class TestFindMatches:

    TEXT = ("the thing about compound interest is that it is boring right up until "
            "the moment it is astonishing and most people quit during the boring part")

    def test_finds_a_term_and_its_timestamp(self):
        count, snippets = find_matches(words(self.TEXT), "astonishing")
        assert count == 1
        # The timestamp must be the matching word's own start, since the player
        # seeks straight to it.
        expected = self.TEXT.split().index("astonishing") * 0.5
        assert snippets[0]["start"] == pytest.approx(expected)

    def test_snippet_includes_surrounding_context(self):
        _, snippets = find_matches(words(self.TEXT), "astonishing")
        assert "compound" in snippets[0]["text"] or "moment" in snippets[0]["text"]
        assert "astonishing" in snippets[0]["text"]

    def test_accent_insensitive_match(self):
        count, snippets = find_matches(words("c'est de la rétro-ingénierie pure"), "retro")
        assert count == 1 and snippets

    def test_multiple_hits_counted(self):
        count, _ = find_matches(words("boring and boring and boring"), "boring")
        assert count == 3

    def test_nearby_hits_are_one_snippet(self):
        """A dense passage is one moment in the conversation, not three."""
        _, snippets = find_matches(words("boring boring boring"), "boring")
        assert len(snippets) == 1

    def test_distant_hits_are_separate_snippets(self):
        filler = " ".join(["filler"] * 60)
        _, snippets = find_matches(words(f"boring {filler} boring"), "boring")
        assert len(snippets) == 2

    def test_snippets_capped(self):
        filler = " ".join(["filler"] * 60)
        text = filler.join([" boring "] * 6)
        _, snippets = find_matches(words(text), "boring", max_snippets=3)
        assert len(snippets) <= 3

    def test_snippets_returned_in_playback_order(self):
        filler = " ".join(["filler"] * 60)
        _, snippets = find_matches(words(f"boring {filler} boring {filler} boring"), "boring")
        assert [s["start"] for s in snippets] == sorted(s["start"] for s in snippets)

    def test_prefers_passage_covering_more_query_terms(self):
        filler = " ".join(["filler"] * 60)
        text = f"alpha {filler} alpha beta {filler} alpha"
        _, snippets = find_matches(words(text), "alpha beta", max_snippets=1)
        assert "beta" in snippets[0]["text"]

    def test_substring_fallback_finds_inflections(self):
        """'engineer' should still find 'engineering'."""
        count, _ = find_matches(words("this is reverse engineering work"), "engineer")
        assert count == 1

    def test_no_match_returns_nothing(self):
        assert find_matches(words(self.TEXT), "zzzznope") == (0, [])

    def test_empty_query(self):
        assert find_matches(words(self.TEXT), "  ") == (0, [])

    def test_malformed_words_json_is_not_fatal(self):
        assert find_matches("not json", "anything") == (0, [])

    def test_ellipsis_marks_truncated_context(self):
        filler = " ".join(["filler"] * 40)
        _, snippets = find_matches(words(f"{filler} needle {filler}"), "needle")
        assert snippets[0]["text"].startswith("…")
        assert snippets[0]["text"].endswith("…")


class TestSearchEndpoint:

    @pytest.mark.asyncio
    async def test_short_query_returns_nothing(self, client):
        """A single character matches most of a library; not worth the scan."""
        assert (await client.get("/search/transcripts?q=a")).json() == []
        assert (await client.get("/search/transcripts?q=")).json() == []

    @pytest.mark.asyncio
    async def test_no_match_is_empty(self, client):
        assert (await client.get("/search/transcripts?q=zzzznope")).json() == []

    @pytest.mark.asyncio
    async def test_punctuation_query_does_not_500(self, client):
        """Raw FTS5 syntax in the box must not surface as a server error."""
        for q in ("a OR b", "((", 'x"y', "-foo", "*"):
            r = await client.get("/search/transcripts", params={"q": q})
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_limit_clamped(self, client):
        r = await client.get("/search/transcripts?q=test&limit=99999")
        assert r.status_code == 200
        assert len(r.json()) <= 30
