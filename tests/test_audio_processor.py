"""Building the clean cut: what goes, what stays, and what it reports.

The ffmpeg calls themselves are not exercised here — they need real audio and
minutes of CPU. What is tested is everything that decides *where* the cuts fall,
because that is what the stored mapping is made of, and a wrong mapping puts
every distill, bookmark and chapter jump in the wrong place.
"""
import pytest

from services import audio_processor as ap
from services import timeline as tl


class TestParseSilence:

    def test_pairs_boundaries(self):
        out = ap.parse_silence(
            "[silencedetect @ 0x1] silence_start: 10.5\n"
            "[silencedetect @ 0x1] silence_end: 12.75 | silence_duration: 2.25\n"
        )
        assert out == [[10.5, 12.75]]

    def test_a_file_ending_quietly_has_an_unterminated_span(self):
        """ffmpeg prints a start with no end. Its length is unknown here, so it
        is dropped rather than guessed at."""
        out = ap.parse_silence(
            "silence_start: 10.0\nsilence_end: 12.0\nsilence_start: 90.0\n"
        )
        assert out == [[10.0, 12.0]]

    def test_nothing_detected(self):
        assert ap.parse_silence("") == []
        assert ap.parse_silence("frame= 100 fps=0.0") == []

    def test_a_negative_start_is_clamped(self):
        assert ap.parse_silence("silence_start: -0.02\nsilence_end: 1.0\n") == [[0.0, 1.0]]


class TestKeepSegments:

    def test_no_cuts_keeps_everything(self):
        assert ap.build_keep_segments(600.0) == [[0.0, 600.0]]

    def test_an_ad_is_removed_with_air_either_side(self):
        """A model's boundary lands on a word, not on the edit."""
        assert ap.build_keep_segments(1180.0, ads=[{"start": 95, "end": 155}]) == [
            [0.0, 94.0], [156.0, 1180.0]]

    def test_a_pause_is_shortened_not_closed(self):
        """Speech with every gap closed is exhausting, and a question runs into
        its answer. A beat of each pause survives.

        Measured on what is *heard*: the kept audio either side of the pause,
        which is what butts together in the cut. The distance between the two
        spans in the original is the part removed, and is meant to be large."""
        silence = [10.0, 14.0]
        segments = ap.build_keep_segments(100.0, silence=[silence])
        heard = (segments[0][1] - silence[0]) + (silence[1] - segments[1][0])
        assert heard == pytest.approx(ap.PAUSE_KEPT_SECONDS, abs=0.01)
        assert segments[1][0] - segments[0][1] == pytest.approx(3.78, abs=0.01)

    def test_a_pause_too_short_to_be_worth_a_seam_is_left(self):
        assert ap.build_keep_segments(100.0, silence=[[10.0, 10.4]]) == [[0.0, 100.0]]

    def test_ads_and_pauses_together(self):
        segments = ap.build_keep_segments(
            600.0, ads=[{"start": 100, "end": 160}], silence=[[300.0, 302.0]])
        assert len(segments) == 3
        assert tl.kept_duration(segments) < 600.0

    def test_overlapping_cuts_do_not_double_count(self):
        """A pause inside an ad is one cut, not two."""
        segments = ap.build_keep_segments(
            600.0, ads=[{"start": 100, "end": 200}], silence=[[120.0, 130.0]])
        assert segments == [[0.0, 99.0], [201.0, 600.0]]

    def test_a_malformed_ad_is_skipped_rather_than_fatal(self):
        assert ap.build_keep_segments(600.0, ads=[{"start": "x"}, {}]) == [[0.0, 600.0]]

    def test_slivers_are_dropped(self):
        """A fragment that short is a click, and costs an ffmpeg call."""
        segments = ap.build_keep_segments(
            600.0, ads=[{"start": 0, "end": 5}, {"start": 5.2, "end": 300}])
        assert all(end - start >= 0.5 for start, end in segments)

    def test_an_unknown_duration_produces_nothing(self):
        assert ap.build_keep_segments(0.0, ads=[{"start": 1, "end": 2}]) == []

    def test_the_result_is_a_usable_mapping(self):
        segments = ap.build_keep_segments(1180.0, ads=[{"start": 95, "end": 155}])
        # Where the cut resumes is where the original resumed, less what went.
        assert tl.to_original(segments, 94.0) == 94.0
        assert tl.to_original(segments, 100.0) == pytest.approx(162.0)
        assert tl.removed_duration(segments, 1180.0) == pytest.approx(62.0)


class TestProcess:
    """The orchestration, with the ffmpeg work stubbed."""

    @pytest.fixture
    def stub(self, monkeypatch, tmp_path):
        calls = {}

        def fake_render(audio_path, segments, output_path, normalize=False):
            calls["segments"] = segments
            calls["normalize"] = normalize
            Path = type(tmp_path)
            Path(output_path).write_bytes(b"audio")
            # render() reports the duration each span actually became.
            lengths = [end - start for start, end in segments]
            calls["rendered_total"] = sum(lengths)
            return lengths

        # The source is 1180s; the finished cut is however long the parts came
        # out, which is what `process` scales the mapping to.
        def durations(path):
            if str(path).endswith("out.mp3"):
                return calls.get("rendered_total", 0.0)
            return 1180.0

        monkeypatch.setattr(ap, "probe_duration", durations)
        monkeypatch.setattr(ap, "detect_silence", lambda p, **k: [[300.0, 305.0]])
        monkeypatch.setattr(ap, "render", fake_render)
        return calls

    def test_reports_the_mapping_and_what_it_saved(self, stub, tmp_path):
        out = ap.process(str(tmp_path / "in.mp3"), str(tmp_path / "out.mp3"),
                         ads=[{"start": 95, "end": 155}], trim_silence=True)
        assert out["segments"] == stub["segments"]
        assert out["removed_seconds"] > 60
        assert out["silence_spans"] == 1
        assert out["total_duration"] == 1180.0

    def test_silence_is_only_measured_when_asked_for(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ap, "probe_duration", lambda p: 600.0)
        monkeypatch.setattr(ap, "render", lambda a, segs, o, **k: [e - s for s, e in segs])
        called = []
        monkeypatch.setattr(ap, "detect_silence", lambda p, **k: called.append(1) or [])
        ap.process(str(tmp_path / "in.mp3"), str(tmp_path / "out.mp3"),
                   ads=[{"start": 10, "end": 100}], trim_silence=False)
        assert not called, "measured silence for a podcast that did not ask for it"

    def test_nothing_to_do_returns_nothing(self, monkeypatch, tmp_path):
        """No ads, no trimming, no levelling — so no second copy of the audio."""
        monkeypatch.setattr(ap, "probe_duration", lambda p: 600.0)
        monkeypatch.setattr(ap, "render", lambda *a, **k: pytest.fail("rendered anyway"))
        assert ap.process(str(tmp_path / "in.mp3"), str(tmp_path / "out.mp3")) is None

    def test_levelling_alone_is_worth_an_encode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ap, "probe_duration", lambda p: 600.0)
        rendered = {}

        def fake(audio, segs, out, normalize=False):
            rendered["n"] = normalize
            return [e - s for s, e in segs]

        monkeypatch.setattr(ap, "render", fake)
        out = ap.process(str(tmp_path / "in.mp3"), str(tmp_path / "out.mp3"), normalize=True)
        assert out is not None and out["removed_seconds"] == 0.0
        assert rendered["n"] is True

    def test_the_mapping_matches_the_audio_actually_written(self, monkeypatch, tmp_path):
        """Cutting an MP3 lands on frame boundaries and concatenating pads the
        joins, so the pieces come out longer than they were asked for. Left
        uncorrected, an episode with two hundred shortened pauses drifts by
        seconds and the read-along ends up on the wrong sentence."""
        source, output = str(tmp_path / "in.mp3"), str(tmp_path / "out.mp3")

        def durations(path):
            return 100.0 if path == source else 33.0     # the finished file

        monkeypatch.setattr(ap, "probe_duration", durations)
        monkeypatch.setattr(ap, "detect_silence", lambda p, **k: [])
        # Asked for 10s + 20s; ffmpeg wrote 11s + 22s, then the concat reported 33s.
        monkeypatch.setattr(ap, "render", lambda *a, **k: [11.0, 22.0])
        monkeypatch.setattr(ap, "build_keep_segments",
                            lambda *a, **k: [[0.0, 10.0], [50.0, 70.0]])

        out = ap.process(source, output, ads=[{"start": 10, "end": 50}])
        mapped = sum(end - start for start, end in out["segments"])
        assert mapped == pytest.approx(33.0, abs=0.01), "mapping does not match the file"
        # And each span still starts where it does in the original.
        assert [s[0] for s in out["segments"]] == [0.0, 50.0]

    def test_an_unreadable_file_is_not_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ap, "probe_duration", lambda p: 0.0)
        assert ap.process(str(tmp_path / "missing.mp3"), str(tmp_path / "out.mp3")) is None
