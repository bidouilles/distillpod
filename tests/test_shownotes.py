"""
Episode descriptions flattened for model context.

The value is in what survives: guest names, handles and URLs live in href
attributes, so a naive tag-strip would silently drop every link — exactly the
thing a listener asks about afterwards.
"""
import pytest

from shownotes import to_text


class TestLinks:

    def test_href_is_preserved_beside_its_text(self):
        out = to_text('<a href="https://x.com/dhh">DHH\'s X</a>')
        assert out == "DHH's X (https://x.com/dhh)"

    def test_bare_url_link_not_duplicated(self):
        out = to_text('<a href="https://omarchy.org">https://omarchy.org</a>')
        assert out == "https://omarchy.org"

    def test_trailing_slash_difference_still_deduped(self):
        out = to_text('<a href="https://omarchy.org/">https://omarchy.org</a>')
        assert out == "https://omarchy.org/"

    def test_link_with_nested_markup(self):
        out = to_text('<a href="https://x.com/dhh"><b>DHH</b></a>')
        assert out == "DHH (https://x.com/dhh)"

    def test_empty_link_text_falls_back_to_url(self):
        assert to_text('<a href="https://a.example"></a>') == "https://a.example"

    def test_multiple_links(self):
        out = to_text('<a href="https://a.example">A</a> and <a href="https://b.example">B</a>')
        assert "A (https://a.example)" in out and "B (https://b.example)" in out


class TestStructure:

    def test_paragraphs_separated_by_a_blank_line(self):
        assert to_text("<p>One</p><p>Two</p>") == "One\n\nTwo"

    def test_br_becomes_newline(self):
        assert to_text("One<br />Two").splitlines() == ["One", "Two"]

    def test_entities_unescaped(self):
        assert to_text("Rails &#8211; 37signals &amp; more") == "Rails – 37signals & more"

    def test_remaining_tags_stripped(self):
        assert to_text("<b>Bold</b> and <i>italic</i>") == "Bold and italic"

    def test_runs_of_blank_lines_collapsed(self):
        assert "\n\n\n" not in to_text("A<br /><br /><br /><br />B")

    def test_empty_and_none(self):
        assert to_text(None) == ""
        assert to_text("") == ""
        assert to_text("<p></p>") == ""


class TestTruncation:

    def test_long_notes_truncated_with_marker(self):
        out = to_text("<p>" + ("word " * 4000) + "</p>", max_chars=200)
        assert len(out) < 400
        assert out.endswith("…(show notes truncated)")

    def test_short_notes_untouched(self):
        assert to_text("<p>Short</p>", max_chars=200) == "Short"


class TestRealFeedSample:
    """Shape taken from the actual Lex Fridman #501 description."""

    SAMPLE = (
        '<p>DHH is the creator of Ruby on Rails.<br />\n'
        'Thank you for listening &#10084; Check out our sponsors: '
        '<a href="https://lexfridman.com/sponsors/ep501-sc">https://lexfridman.com/sponsors/ep501-sc</a></p>'
        '<p><b>EPISODE LINKS:</b><br />\n'
        'DHH&#8217;s X: <a href="https://x.com/dhh">https://x.com/dhh</a><br />\n'
        'DHH&#8217;s Blog: <a href="https://world.hey.com/dhh">https://world.hey.com/dhh</a></p>'
    )

    def test_names_and_urls_survive(self):
        out = to_text(self.SAMPLE)
        assert "Ruby on Rails" in out
        assert "https://x.com/dhh" in out
        assert "https://world.hey.com/dhh" in out
        assert "DHH’s Blog" in out

    def test_no_markup_leaks(self):
        out = to_text(self.SAMPLE)
        assert "<" not in out and "&#" not in out


class TestChatPromptWiring:

    def test_notes_are_labelled_as_unspoken(self):
        """Unlabelled notes would read as things a guest said on air."""
        from routers.chat import _notes_block
        block = _notes_block("DHH's Blog (https://world.hey.com/dhh)")
        assert "not spoken" in block
        assert "world.hey.com/dhh" in block

    def test_absent_notes_add_nothing_to_the_prompt(self):
        from routers.chat import _notes_block
        assert _notes_block("") == ""
