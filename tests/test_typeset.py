"""Typesetting a research report.

A third renderer over the stored structure, alongside the page and the Markdown.
The risk worth testing is not the layout — it is that arbitrary transcript text
and web-page titles reach a markup language: a quote containing `#` or `[…]`
must not become syntax, and a missing binary must cost nothing but the button.
"""
import json
import shutil
from pathlib import Path

import pytest

from services import typeset

HAS_TYPST = shutil.which("typst") is not None

DATA = {
    "claim": "Do repeated runs find vulnerabilities a single run misses?",
    "report": {
        "verdict": "mixed",
        "verdict_note": "Supported in principle [2], unproven in specifics [4].",
        "sections": [
            {"heading": "What the sources establish",
             "body": "Three successes in ten identical attempts [2]."},
            {"heading": "What is contested",
             "body": "Two vendors call the impact overstated [8]."},
        ],
        "open_questions": ["How many were exploitable?"],
    },
    "sources": [
        {"title": "Evaluation report", "url": "https://example.gov.uk/report",
         "published": "2026-04-02", "query": "evaluation"},
        {"title": "Rebuttal", "url": "https://vendor.example.com/blog", "published": ""},
    ],
    "echoes": [{"podcast_title": "Low Level", "episode_title": "Who Allowed This?",
                "start": 134.0, "text": "it'll give you a list of vulnerabilities"}],
    "episode": {"title": "The episode", "podcast_title": "The show"},
    "quote": "a quote from the transcript",
    "queries": ["evaluation", "severity"],
}


class TestPayload:

    def test_sources_are_numbered_with_domain_and_date(self):
        out = typeset.payload(DATA)
        assert out["sources"][0]["meta"].startswith("example.gov.uk · 2026-04-02")
        assert out["sources"][1]["meta"] == "vendor.example.com"

    def test_the_verdict_is_named_not_coded(self):
        assert typeset.payload(DATA)["verdict_label"] == "Mixed evidence"

    def test_an_unknown_verdict_does_not_break_the_note(self):
        data = {**DATA, "report": {**DATA["report"], "verdict": "banana"}}
        out = typeset.payload(data)
        assert out["verdict"] == "banana" and out["verdict_label"] == "Findings"

    def test_empty_sections_are_dropped(self):
        data = {**DATA, "report": {**DATA["report"],
                                   "sections": [{"heading": "Empty", "body": "  "}]}}
        assert typeset.payload(data)["sections"] == []

    def test_a_long_quote_is_trimmed(self):
        data = {**DATA, "quote": " ".join(["word"] * 300)}
        assert typeset.payload(data)["quote"].endswith("…")

    def test_headings_follow_the_language(self):
        french = {**DATA, "claim": "Les modèles qui sont dans une cible avec des failles pour nous"}
        assert typeset.payload(french)["lang"] == "fr"
        assert "Ailleurs" in typeset.payload(french)["labels"]["echoes"]
        assert typeset.payload(DATA)["lang"] == "en"

    def test_the_footer_says_what_it_was_built_from(self):
        assert "2 web sources" in typeset.payload(DATA)["footer"]

    def test_a_report_with_nothing_in_it_still_renders_a_payload(self):
        out = typeset.payload({})
        assert out["claim"] and out["sources"] == [] and out["sections"] == []


class TestAvailability:

    def test_no_binary_means_no_pdf(self, monkeypatch, tmp_path):
        monkeypatch.setattr(typeset.shutil, "which", lambda name: None)
        assert typeset.available() is False
        assert typeset.render(DATA, tmp_path / "out.pdf") is None

    def test_a_missing_template_is_not_a_crash(self, monkeypatch, tmp_path):
        monkeypatch.setattr(typeset, "TEMPLATE", tmp_path / "absent.typ")
        assert typeset.available() is False

    def test_a_failing_compile_returns_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(typeset, "available", lambda: True)
        monkeypatch.setattr(typeset, "binary", lambda: "/bin/false")
        assert typeset.render(DATA, tmp_path / "out.pdf") is None


@pytest.mark.skipif(not HAS_TYPST, reason="typst is not installed here")
class TestRealCompile:
    """The template is only as good as what it does with real input."""

    def test_it_produces_a_pdf(self, tmp_path):
        out = typeset.render(DATA, tmp_path / "report.pdf")
        assert out and out.exists()
        assert out.read_bytes()[:4] == b"%PDF"

    def test_markup_in_the_content_cannot_break_the_document(self, tmp_path):
        """Transcript quotes and page titles are arbitrary text reaching a
        markup language. `#` starts code in Typst; `[` opens content."""
        hostile = {
            **DATA,
            "claim": "A claim with # and [brackets] and *stars* and $math$",
            "quote": "#set page(width: 1pt) — an injection attempt",
            "report": {**DATA["report"],
                       "sections": [{"heading": "With #hash",
                                     "body": "Body with [content] and _underscores_ and \\\\ backslash"}]},
            "sources": [{"title": "Title with #hash and [brackets]",
                         "url": "https://x.example/a", "published": ""}],
        }
        out = typeset.render(hostile, tmp_path / "hostile.pdf")
        assert out and out.read_bytes()[:4] == b"%PDF"

    def test_french_text_renders(self, tmp_path):
        french = {**DATA,
                  "claim": "Les IA capables de découvrir des failles dans une cible",
                  "quote": "Ça n'est pas suffisant — même à la quatorzième tentative."}
        out = typeset.render(french, tmp_path / "fr.pdf")
        assert out and out.read_bytes()[:4] == b"%PDF"
