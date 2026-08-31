"""
Unit tests for suggest-podcasts.py helper functions.
"""
import importlib.util
import sqlite3
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "suggest-podcasts.py")
spec = importlib.util.spec_from_file_location("suggest_podcasts", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# ── get_search_queries() ─────────────────────────────────────────────────────
#
# The CLI invocation and fence-stripping this file used to cover now live in
# services/llm.py and are tested in test_llm.py. What is left here is the
# script's own contract with the adapter.

class TestGetSearchQueries:

    def _run(self, reply, **kwargs):
        with patch.object(mod.llm, "run_json", return_value=reply) as p:
            return mod.get_search_queries("some context"), p

    def test_parses_queries(self):
        result, _ = self._run({"queries": ["q1", "q2", "q3", "q4"]})
        assert result == ["q1", "q2", "q3", "q4"]

    def test_constrains_reply_with_a_schema(self):
        _, p = self._run({"queries": ["q1"]})
        assert p.call_args.kwargs["schema"] == mod.QUERIES_SCHEMA

    def test_caps_at_n_suggest(self):
        result, _ = self._run({"queries": [f"q{i}" for i in range(10)]})
        assert len(result) == mod.N_SUGGEST

    def test_coerces_to_strings(self):
        result, _ = self._run({"queries": ["q1", 2]})
        assert result == ["q1", "2"]

    def test_raises_when_no_queries_returned(self):
        import pytest
        with pytest.raises(ValueError, match="no search queries"):
            self._run({"queries": []})


class TestGetReason:

    def test_strips_trailing_period(self):
        with patch.object(mod.llm, "run", return_value="Deep AI research interviews.\n"):
            reason = mod.get_reason(["Show A"], {"title": "Show B"})
        assert reason == "Deep AI research interviews"


# ── Deduplication / filtering ─────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE subscriptions (podcast_id TEXT PRIMARY KEY, feed_url TEXT, title TEXT, subscribed_at TEXT);
CREATE TABLE suggestions   (id TEXT PRIMARY KEY, feed_url TEXT, dismissed INTEGER DEFAULT 0);
"""

def _make_db(seed_sql=""):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.executescript(SCHEMA)
    if seed_sql:
        conn.executescript(seed_sql)
    conn.commit()
    conn.close()
    return f.name


class TestFiltering:

    def test_subscribed_feed_excluded(self):
        db_path = _make_db(
            "INSERT INTO subscriptions VALUES ('p1','https://feeds.example.com/subscribed','My Show','2026-01-01');"
        )
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        urls = mod.get_subscribed_feed_urls(db)
        db.close()
        os.unlink(db_path)
        assert "https://feeds.example.com/subscribed" in urls

    def test_existing_suggestion_excluded(self):
        db_path = _make_db(
            "INSERT INTO suggestions VALUES ('s1','https://feeds.example.com/already', 0);"
        )
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        urls = mod.get_existing_suggestion_feed_urls(db)
        db.close()
        os.unlink(db_path)
        assert "https://feeds.example.com/already" in urls

    def test_dismissed_suggestion_still_excluded(self):
        # dismissed=1 should still be in the exclusion set (avoid re-suggesting dismissed)
        db_path = _make_db(
            "INSERT INTO suggestions VALUES ('s1','https://feeds.example.com/dismissed', 1);"
        )
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        urls = mod.get_existing_suggestion_feed_urls(db)
        db.close()
        os.unlink(db_path)
        assert "https://feeds.example.com/dismissed" in urls

    def test_empty_db_returns_empty_sets(self):
        db_path = _make_db()
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        assert mod.get_subscribed_feed_urls(db) == set()
        assert mod.get_existing_suggestion_feed_urls(db) == set()
        db.close()
        os.unlink(db_path)

    def test_itunes_search_skips_no_feed_url(self):
        """search_itunes should skip results with no feedUrl."""
        fake_response = {
            "results": [
                {"collectionId": 1, "collectionName": "No Feed", "artistName": "X",
                 "collectionCensoredName": "desc"},  # missing feedUrl
                {"collectionId": 2, "collectionName": "Has Feed", "artistName": "Y",
                 "collectionCensoredName": "desc2", "feedUrl": "https://feeds.example.com/ok",
                 "artworkUrl600": "https://img.example.com/art.jpg"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = mock_resp
            results = mod.search_itunes("test query", limit=5)

        assert len(results) == 1
        assert results[0]["title"] == "Has Feed"
        assert results[0]["feed_url"] == "https://feeds.example.com/ok"
