"""Mapping between the original audio and a cut of it.

This is the specification for both `backend/services/timeline.py` and its
mirror in `frontend/src/lib/timeline.ts`. The cut runs on a different clock, and
before this existed everything timestamp-driven was wrong by however much had
been removed — a distill on the ad-free version captured a passage the listener
never heard.
"""
import pytest

from services import timeline as tl

# One ad removed: 95s–155s of a 1180s episode.
SEGMENTS = [[0.0, 95.0], [155.0, 1180.0]]
# Two removed, including a pre-roll: nothing kept before 30s.
PREROLL = [[30.0, 100.0], [160.0, 600.0]]


class TestParse:

    def test_reads_a_stored_list(self):
        assert tl.parse("[[0, 95], [155, 1180]]") == SEGMENTS

    def test_nothing_stored(self):
        assert tl.parse(None) is None
        assert tl.parse("") is None

    def test_rubbish_is_not_fatal(self):
        """A broken mapping must not make an episode unplayable — the caller
        falls back to treating the clocks as identical."""
        assert tl.parse("not json") is None
        assert tl.parse("{}") is None
        assert tl.parse("[]") is None

    def test_junk_entries_are_dropped(self):
        assert tl.parse('[[0, 10], "x", [5], [20, 15], [30, 40]]') == [[0.0, 10.0], [30.0, 40.0]]

    def test_spans_are_sorted_and_merged(self):
        assert tl.normalise([[155, 1180], [0, 95]]) == SEGMENTS
        assert tl.normalise([[0, 100], [50, 200]]) == [[0.0, 200.0]]


class TestToOriginal:
    """A position in the cut → where it is in the original."""

    def test_before_the_first_cut_is_unchanged(self):
        assert tl.to_original(SEGMENTS, 40.0) == 40.0

    def test_after_a_cut_is_shifted_by_what_was_removed(self):
        # 60s of ad removed, so cut-100s is original-160s.
        assert tl.to_original(SEGMENTS, 100.0) == 160.0

    def test_the_join_itself(self):
        assert tl.to_original(SEGMENTS, 95.0) == 95.0

    def test_a_preroll_shifts_everything(self):
        assert tl.to_original(PREROLL, 0.0) == 30.0
        assert tl.to_original(PREROLL, 10.0) == 40.0

    def test_past_the_end_gives_the_end(self):
        assert tl.to_original(SEGMENTS, 99_999.0) == 1180.0

    def test_no_mapping_is_the_identity(self):
        assert tl.to_original(None, 123.0) == 123.0

    def test_negative_is_clamped(self):
        assert tl.to_original(SEGMENTS, -5.0) == 0.0


class TestToCut:
    """A position in the original → where it is in the cut."""

    def test_before_the_first_cut_is_unchanged(self):
        assert tl.to_cut(SEGMENTS, 40.0) == 40.0

    def test_after_a_cut_is_shifted(self):
        assert tl.to_cut(SEGMENTS, 160.0) == 100.0

    def test_inside_a_removed_span_snaps_forward(self):
        """A chapter mark landing inside a sponsor read should start the
        chapter, not replay the end of the previous one."""
        assert tl.to_cut(SEGMENTS, 120.0) == 95.0

    def test_before_a_preroll_snaps_to_the_start(self):
        assert tl.to_cut(PREROLL, 5.0) == 0.0

    def test_after_everything_kept(self):
        assert tl.to_cut(PREROLL, 5000.0) == tl.kept_duration(PREROLL)

    def test_no_mapping_is_the_identity(self):
        assert tl.to_cut(None, 123.0) == 123.0


class TestRoundTrip:

    @pytest.mark.parametrize("cut_at", [0.0, 1.0, 94.9, 95.0, 95.1, 300.0, 1000.0])
    def test_cut_to_original_and_back(self, cut_at):
        """Every position in the cut has an exact place in the original."""
        assert tl.to_cut(SEGMENTS, tl.to_original(SEGMENTS, cut_at)) == pytest.approx(cut_at)

    def test_durations(self):
        assert tl.kept_duration(SEGMENTS) == 95.0 + 1025.0
        assert tl.removed_duration(SEGMENTS, 1180.0) == 60.0
        assert tl.removed_duration(None, 1180.0) == 0.0
