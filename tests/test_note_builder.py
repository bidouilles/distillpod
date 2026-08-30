"""
Unit tests for the Obsidian export (backend/services/note_builder.py).

The note goes into someone's vault, so the bar is that it is always valid
Markdown and any diagram in it always renders. The model supplies labels, not
syntax, and these tests hold that line.
"""
import json

import pytest

from services import note_builder as nb


EPISODE = {
    "title": "Would rust have fixed this?",
    "audio_url": "https://www.youtube.com/watch?v=uQV6hYwyjMY",
    "podcast_title": "Low Level",
    "published_at": "2026-08-28T15:55:13+00:00",
    "duration_seconds": 736,
    "summary": "A supply chain attack on a Rust crate.",
}


class TestMermaid:

    def test_a_map_becomes_a_flowchart(self):
        out = nb.render_mermaid({
            "root": "Supply chain attack",
            "branches": [{"label": "Mechanism", "children": ["build.rs runs at compile time"]}],
        })
        assert out.startswith("flowchart TD")
        assert '"Supply chain attack"' in out
        assert "-->" in out

    def test_quotes_and_brackets_are_stripped_from_labels(self):
        """They would terminate the node early and break the whole diagram."""
        out = nb.render_mermaid({
            "root": 'He said "hello" [really]',
            "branches": [{"label": "a (b) {c}", "children": ["x|y"]}],
        })
        # Every node label is exactly one quoted string, so nothing can
        # terminate a node early and derail the parse.
        for line in out.splitlines():
            if '["' in line:
                assert line.count('"') == 2
                inner = line.split('"')[1]
                assert not set(inner) & set('"[]{}()<>|')

    def test_a_newline_in_a_label_cannot_break_out(self):
        out = nb.render_mermaid({"root": "one\ntwo", "branches": [{"label": "b", "children": []}]})
        assert "one two" in out
        assert len([l for l in out.splitlines() if 'N0["' in l]) == 1

    @pytest.mark.parametrize("bad", [
        {}, {"root": "", "branches": []}, {"root": "x", "branches": []},
        {"root": "x", "branches": [{"label": "", "children": []}]},
    ])
    def test_an_unusable_map_renders_nothing(self, bad):
        """Better no diagram than a broken one."""
        assert nb.render_mermaid(bad) == ""

    def test_the_diagram_is_bounded(self):
        big = {"root": "r", "branches": [
            {"label": f"b{i}", "children": [f"c{j}" for j in range(20)]} for i in range(20)
        ]}
        out = nb.render_mermaid(big)
        assert out.count("-->") <= 5 + 5 * 4


class TestDeepLinks:

    def test_youtube_gets_a_timestamped_link(self):
        assert nb._deep_link("https://www.youtube.com/watch?v=abc", 272) == \
            "https://www.youtube.com/watch?v=abc&t=272s"

    def test_a_url_without_a_query_still_works(self):
        assert nb._deep_link("https://youtu.be/abc", 10) == "https://youtu.be/abc?t=10s"

    def test_a_podcast_mp3_gets_no_link(self):
        """A link that silently ignores its timestamp is worse than plain text."""
        assert nb._deep_link("https://cdn.example.com/ep1.mp3", 100) is None


class TestMarkdown:

    def test_the_note_carries_frontmatter_and_a_title(self):
        md = nb.build_markdown(EPISODE, [], [])
        assert md.startswith("---\n")
        assert 'title: "Would rust have fixed this?"' in md
        assert "# Would rust have fixed this?" in md
        assert "tags: [podcast, distillpod]" in md

    def test_a_quote_in_the_title_cannot_break_the_frontmatter(self):
        md = nb.build_markdown({**EPISODE, "title": 'He said "no"'}, [], [])
        head = md.split("---")[1]
        assert 'title: "He said \\"no\\""' in head

    def test_empty_sections_are_left_out(self):
        md = nb.build_markdown({**EPISODE, "summary": None}, [], [])
        for heading in ("## Summary", "## Highlights", "## Chapters", "## Key points", "## Mentioned", "## Map"):
            assert heading not in md

    def test_highlights_carry_the_quote_the_insight_and_the_time(self):
        gists = [{"start_seconds": 272, "text": "raw excerpt",
                  "summary": json.dumps({"quote": "the quotable bit", "insight": "why it matters"}),
                  "auto": 0}]
        md = nb.build_markdown(EPISODE, [], gists)
        assert "> the quotable bit" in md
        assert "why it matters" in md
        assert "[4:32](https://www.youtube.com/watch?v=uQV6hYwyjMY&t=272s)" in md

    def test_an_auto_highlight_says_so(self):
        gists = [{"start_seconds": 10, "text": "t", "summary": None, "auto": 1}]
        assert "*(auto)*" in nb.build_markdown(EPISODE, [], gists)

    def test_a_highlight_with_no_ai_summary_falls_back_to_the_excerpt(self):
        gists = [{"start_seconds": 10, "text": "what was actually said", "summary": None, "auto": 0}]
        assert "> what was actually said" in nb.build_markdown(EPISODE, [], gists)

    def test_chapters_become_a_timestamped_list(self):
        md = nb.build_markdown(EPISODE, [{"title": "Intro", "start_time": 0}], [])
        assert "## Chapters" in md
        assert "— Intro" in md

    def test_extras_render_every_section(self):
        extras = {
            "key_points": ["Rust does not stop supply chain attacks."],
            "mentioned": [{"name": "cargo-audit", "kind": "tool",
                           "detail": "scans dependencies", "start_seconds": 252}],
            "map": {"root": "Attack", "branches": [{"label": "How", "children": ["build.rs"]}]},
        }
        md = nb.build_markdown(EPISODE, [], [], extras)
        assert "## Key points" in md and "- Rust does not stop" in md
        assert "## Mentioned" in md and "cargo-audit" in md and "🛠" in md
        assert "## Map" in md and "```mermaid" in md and "flowchart TD" in md

    def test_a_pipe_in_a_mention_cannot_break_the_table(self):
        extras = {"key_points": [], "mentioned": [
            {"name": "a|b", "kind": "tool", "detail": "c|d", "start_seconds": 0}], "map": {}}
        md = nb.build_markdown(EPISODE, [], [], extras)
        row = next(l for l in md.splitlines() if l.startswith("| 🛠"))
        assert "a\\|b" in row and "c\\|d" in row   # escaped, so the cells hold
        assert row.replace("\\|", "").count("|") == 4

    def test_a_podcast_note_shows_times_without_links(self):
        md = nb.build_markdown(
            {**EPISODE, "audio_url": "https://cdn.example.com/ep1.mp3"},
            [{"title": "Intro", "start_time": 65}], [])
        assert "1:05 — Intro" in md
        # The chapter line itself carries no link; the footer source link stays.
        chapter_line = next(l for l in md.splitlines() if "Intro" in l)
        assert "](" not in chapter_line


class TestEnrich:

    def test_no_transcript_means_no_model_call(self):
        from unittest.mock import patch
        with patch.object(nb.llm, "run_json") as run:
            assert nb.enrich("", "t") is None
            assert nb.enrich("[]", "t") is None
        assert not run.called

    def test_a_failed_call_degrades_to_none(self):
        from unittest.mock import patch
        words = json.dumps([{"word": "hi", "start": 0, "end": 1}])
        with patch.object(nb.llm, "run_json", return_value=None):
            assert nb.enrich(words, "t") is None


class TestBrief:
    """The short 'what is this about' shown when an episode is first opened."""

    WORDS = json.dumps([{"word": f" w{i}", "start": i, "end": i + 1} for i in range(200)])

    def test_a_tldr_and_bullets_become_one_stored_string(self):
        from unittest.mock import patch
        reply = {"tldr": "A supply chain attack on a Rust crate.",
                 "bullets": ["build.rs runs at compile time", "rotate credentials"]}
        with patch.object(nb.llm, "run_json", return_value=reply):
            out = nb.brief(self.WORDS, "Would rust have fixed this?")
        assert out.startswith("A supply chain attack")
        assert "• build.rs runs at compile time" in out
        assert "• rotate credentials" in out

    def test_a_tldr_alone_is_enough(self):
        from unittest.mock import patch
        with patch.object(nb.llm, "run_json", return_value={"tldr": "Just this.", "bullets": []}):
            assert nb.brief(self.WORDS, "t") == "Just this."

    def test_an_empty_answer_is_no_summary_rather_than_a_blank_card(self):
        from unittest.mock import patch
        with patch.object(nb.llm, "run_json", return_value={"tldr": "  ", "bullets": []}):
            assert nb.brief(self.WORDS, "t") is None

    def test_no_transcript_means_no_model_call(self):
        from unittest.mock import patch
        with patch.object(nb.llm, "run_json") as run:
            assert nb.brief("[]", "t") is None
        assert not run.called
