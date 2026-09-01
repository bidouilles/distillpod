"""Background work taking turns.

Nine places started background work independently and a cron job did the same
work again from another process, so pressing play while the nightly sync ran
meant two yt-dlp processes against one rate limit and two transcriptions on two
cores. These tests pin the properties that fixes it: one turn at a time per
resource, lanes independent of each other, and whoever a person is waiting on
going first.
"""
import asyncio
import time

import pytest

from services import jobs

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_lanes(monkeypatch, tmp_path):
    jobs.reset()
    # No spacing by default: the delay is real and tested on its own.
    monkeypatch.setattr(jobs, "SPACING", {})
    jobs.set_lock_dir(tmp_path / "locks")
    yield
    jobs.reset()


async def hold(lane: str, name: str, log: list, seconds: float = 0.02, level=None):
    async with jobs.lane(lane, label=name, level=level):
        log.append(f"start {name}")
        await asyncio.sleep(seconds)
        log.append(f"end {name}")


class TestOneAtATime:

    async def test_turns_do_not_overlap(self):
        log: list[str] = []
        await asyncio.gather(*(hold("stt", f"j{i}", log) for i in range(4)))
        # Every start is followed by its own end before the next start.
        for i in range(0, len(log), 2):
            assert log[i].startswith("start") and log[i + 1].startswith("end")
            assert log[i].split()[1] == log[i + 1].split()[1]

    async def test_a_failing_turn_frees_the_lane(self):
        async def boom():
            async with jobs.lane("stt", label="bad"):
                raise RuntimeError("no")

        with pytest.raises(RuntimeError):
            await boom()
        log: list[str] = []
        await hold("stt", "after", log)
        assert log == ["start after", "end after"]

    async def test_lanes_are_independent(self):
        """A play fetching audio must not wait on a transcription."""
        started = time.time()
        await asyncio.gather(
            hold("stt", "transcribe", [], 0.15),
            hold("media", "download", [], 0.15),
        )
        assert time.time() - started < 0.28, "different resources were serialised"


class TestPriority:

    async def test_a_listener_overtakes_housekeeping(self):
        log: list[str] = []
        with jobs.priority_scope(jobs.BACKGROUND):
            blocker = asyncio.create_task(hold("youtube", "nightly-1", log, 0.05))
            await asyncio.sleep(0.005)
            queued = [asyncio.create_task(hold("youtube", f"nightly-{i}", log, 0.01))
                      for i in range(2, 5)]
        await asyncio.sleep(0.005)
        with jobs.priority_scope(jobs.USER):
            play = asyncio.create_task(hold("youtube", "PLAY", log, 0.01))

        await asyncio.gather(blocker, play, *queued)
        starts = [line.split()[1] for line in log if line.startswith("start")]
        # The turn in progress finishes, then the listener goes ahead of the rest.
        assert starts[0] == "nightly-1"
        assert starts[1] == "PLAY", f"housekeeping went first: {starts}"

    async def test_equal_priority_keeps_its_order(self):
        """Fairness, so nothing starves behind a stream of equals."""
        log: list[str] = []
        tasks = []
        for i in range(4):
            tasks.append(asyncio.create_task(hold("llm", f"j{i}", log, 0.01)))
            await asyncio.sleep(0.002)
        await asyncio.gather(*tasks)
        assert [l.split()[1] for l in log if l.startswith("start")] == ["j0", "j1", "j2", "j3"]

    async def test_priority_is_inherited_by_nested_calls(self):
        """A request handler sets it once; six service calls underneath do not
        each need an argument for it."""
        seen = {}

        async def deep():
            seen["level"] = jobs.priority()
            async with jobs.lane("web", label="inner"):
                pass

        with jobs.priority_scope(jobs.USER):
            await deep()
        assert seen["level"] == jobs.USER
        # And it is restored afterwards.
        assert jobs.priority() == jobs.BACKGROUND


class TestSpacing:

    async def test_youtube_turns_are_spaced(self, monkeypatch):
        """Asking yt-dlp for metadata in a tight loop is what got the address
        refused for everything, for hours."""
        monkeypatch.setattr(jobs, "SPACING", {"youtube": 0.12})
        started = time.time()
        await hold("youtube", "a", [], 0.0)
        await hold("youtube", "b", [], 0.0)
        assert time.time() - started >= 0.12

    async def test_other_lanes_are_not_slowed_down(self, monkeypatch):
        monkeypatch.setattr(jobs, "SPACING", {"youtube": 0.2})
        started = time.time()
        await hold("stt", "a", [], 0.0)
        await hold("stt", "b", [], 0.0)
        assert time.time() - started < 0.1


class TestStatus:

    async def test_reports_what_is_running_and_what_waits(self):
        seen = {}

        async def watcher():
            await asyncio.sleep(0.02)
            seen["status"] = jobs.status()

        async def busy():
            async with jobs.lane("stt", label="transcribe: ep-1"):
                await asyncio.sleep(0.06)

        async def queued():
            await asyncio.sleep(0.01)
            async with jobs.lane("stt", label="transcribe: ep-2"):
                pass

        await asyncio.gather(busy(), queued(), watcher())
        lane = seen["status"]["stt"]
        assert lane["running"] == "transcribe: ep-1"
        assert lane["waiting"] == 1
        assert "transcribe: ep-2" in lane["queue"]

    async def test_counts_completed_turns(self):
        await hold("web", "one", [])
        await hold("web", "two", [])
        assert jobs.status()["web"]["completed"] == 2

    async def test_an_idle_lane_reports_nothing_running(self):
        await hold("web", "one", [])
        assert jobs.status()["web"]["running"] is None


class TestCrossProcess:
    """The nightly script is a separate process, so the turns are held through
    lock files both can see."""

    async def test_a_lock_file_is_created_for_the_lane(self, tmp_path):
        jobs.set_lock_dir(tmp_path / "locks")
        await hold("youtube", "x", [])
        assert (tmp_path / "locks" / "youtube.lock").exists()

    async def test_work_still_runs_when_the_lock_cannot_be_made(self, monkeypatch):
        """Failing to do housekeeping because a lock file could not be written
        would be worse than doing it unserialised."""
        monkeypatch.setattr(jobs, "_lock_dir", None)
        log: list[str] = []
        await hold("stt", "unlocked", log)
        assert log == ["start unlocked", "end unlocked"]

    async def test_another_process_holding_the_lane_makes_us_wait(self, tmp_path):
        import fcntl
        jobs.set_lock_dir(tmp_path / "locks")
        (tmp_path / "locks").mkdir(exist_ok=True)
        other = open(tmp_path / "locks" / "stt.lock", "w")
        fcntl.flock(other, fcntl.LOCK_EX)

        done = asyncio.Event()

        async def attempt():
            async with jobs.lane("stt", label="ours"):
                done.set()

        task = asyncio.create_task(attempt())
        await asyncio.sleep(0.3)
        assert not done.is_set(), "ran while another process held the lane"

        fcntl.flock(other, fcntl.LOCK_UN)
        other.close()
        await asyncio.wait_for(task, timeout=5)
        assert done.is_set()


class TestCaptionsFirst:
    """A captioned YouTube video must not be sent to a paid speech-to-text
    backend to re-derive, at length, a transcript YouTube hands over for free.
    The ingest path always preferred captions; the play path did not, so every
    video the nightly caption pass had not reached took the expensive route."""

    async def test_captions_are_used_when_the_video_has_them(self, monkeypatch, tmp_path):
        from services import transcriber

        async def fake_metadata(url):
            return {"webpage_url": url}

        async def fake_captions(meta):
            return [{"word": " hello", "start": 0.0, "end": 0.4}]

        monkeypatch.setattr(transcriber.youtube, "fetch_metadata", fake_metadata)
        monkeypatch.setattr(transcriber.youtube, "fetch_caption_words", fake_captions)
        monkeypatch.setattr(transcriber.youtube, "caption_language", lambda meta: "fr")
        monkeypatch.setattr(transcriber.stt, "transcribe",
                            lambda path: pytest.fail("spent speech-to-text on a captioned video"))

        words, language = await transcriber._obtain_words("yt-abc123", tmp_path / "a.mp3")
        assert words and language == "fr"

    async def test_speech_to_text_still_covers_a_video_without_captions(
        self, monkeypatch, tmp_path,
    ):
        from services import transcriber

        async def fake_metadata(url):
            return {}

        async def no_captions(meta):
            return []

        monkeypatch.setattr(transcriber.youtube, "fetch_metadata", fake_metadata)
        monkeypatch.setattr(transcriber.youtube, "fetch_caption_words", no_captions)
        monkeypatch.setattr(transcriber.stt, "transcribe",
                            lambda path: [{"word": " spoken", "start": 0.0, "end": 0.5}])
        words, _ = await transcriber._obtain_words("yt-abc123", tmp_path / "a.mp3")
        assert words[0]["word"] == " spoken"

    async def test_a_refused_caption_fetch_falls_back_rather_than_failing(
        self, monkeypatch, tmp_path,
    ):
        from services import transcriber

        async def fake_metadata(url):
            raise RuntimeError("429 from YouTube")

        monkeypatch.setattr(transcriber.youtube, "fetch_metadata", fake_metadata)
        monkeypatch.setattr(transcriber.stt, "transcribe",
                            lambda path: [{"word": " spoken", "start": 0.0, "end": 0.5}])
        words, _ = await transcriber._obtain_words("yt-abc123", tmp_path / "a.mp3")
        assert words, "a throttled caption fetch lost the episode entirely"

    async def test_a_podcast_goes_straight_to_speech_to_text(self, monkeypatch, tmp_path):
        """Only YouTube episodes have captions to try."""
        from services import transcriber

        async def fetch(url):
            pytest.fail("asked YouTube about a podcast episode")

        monkeypatch.setattr(transcriber.youtube, "fetch_metadata", fetch)
        monkeypatch.setattr(transcriber.stt, "transcribe",
                            lambda path: [{"word": " spoken", "start": 0.0, "end": 0.5}])
        words, _ = await transcriber._obtain_words("ep_001", tmp_path / "a.mp3")
        assert words


class TestLockRobustness:
    """A lane must not be able to wedge itself. The OS releases a crashed
    process's lock; nothing releases one leaked inside this process."""

    async def test_reset_releases_what_it_forgets(self, tmp_path):
        jobs.set_lock_dir(tmp_path / "locks")
        await hold("stt", "first", [])
        jobs._enter_lock("stt")          # as a leaked turn would leave it
        jobs.reset()
        jobs.set_lock_dir(tmp_path / "locks")
        # Would hang forever if reset had merely forgotten the descriptor.
        await asyncio.wait_for(hold("stt", "second", []), timeout=5)

    async def test_a_stuck_lock_does_not_wedge_the_app_forever(self, tmp_path, monkeypatch):
        import fcntl
        jobs.set_lock_dir(tmp_path / "locks")
        (tmp_path / "locks").mkdir(exist_ok=True)
        stuck = open(tmp_path / "locks" / "llm.lock", "w")
        fcntl.flock(stuck, fcntl.LOCK_EX)
        monkeypatch.setattr(jobs, "LOCK_WAIT_CEILING", 0.6)

        log: list[str] = []
        await asyncio.wait_for(hold("llm", "eventually", log), timeout=5)
        assert log == ["start eventually", "end eventually"]

        fcntl.flock(stuck, fcntl.LOCK_UN)
        stuck.close()


class TestNesting:
    """Taking a lane twice from the same task is a deadlock: the inner turn
    waits for an outer one that cannot finish until the inner one does. It is
    also natural here — fetching captions is one labelled turn whose helper
    takes the same lane on its own behalf — so re-entry has to be a no-op."""

    async def test_a_nested_turn_does_not_wait_for_itself(self):
        async def outer():
            async with jobs.lane("youtube", label="captions: ep-1"):
                async with jobs.lane("youtube", label="video metadata"):
                    return "reached"

        assert await asyncio.wait_for(outer(), timeout=2) == "reached"

    async def test_the_lane_is_free_again_afterwards(self):
        async def outer():
            async with jobs.lane("youtube", label="outer"):
                async with jobs.lane("youtube", label="inner"):
                    pass

        await asyncio.wait_for(outer(), timeout=2)
        log: list[str] = []
        await asyncio.wait_for(hold("youtube", "next", log), timeout=2)
        assert log == ["start next", "end next"]

    async def test_a_different_task_still_waits(self):
        """Re-entry is per task, not a free pass for everyone."""
        order: list[str] = []

        async def outer():
            async with jobs.lane("youtube", label="outer"):
                order.append("outer in")
                await asyncio.sleep(0.08)
                async with jobs.lane("youtube", label="nested"):
                    order.append("nested")
                order.append("outer out")

        async def other():
            await asyncio.sleep(0.01)
            async with jobs.lane("youtube", label="other"):
                order.append("other")

        await asyncio.gather(outer(), other())
        assert order == ["outer in", "nested", "outer out", "other"]
