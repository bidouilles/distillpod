"""Background work, one thing at a time per resource.

Nine places in this app start background work with `asyncio.create_task`, and a
cron job outside it does the same work again. Nothing coordinated them, so
pressing play while the nightly sync ran meant two yt-dlp processes competing
for the same rate limit, two transcriptions competing for two cores, and a
listener watching a spinner because the box was busy with housekeeping nobody
asked for.

The fix is a turn-taking lane per resource rather than one global queue. What
must not overlap is work that shares a *constraint*:

    youtube   yt-dlp calls — YouTube rate-limits by address, and being refused
              costs hours, so turns here are also spaced apart
    media     audio downloads — bandwidth, and the first file finishing sooner
              is what someone waiting to play actually wants
    stt       transcription — a hosted call that bills, or a core it pins
    llm       the agent CLI — two of those on a two-core box is neither
    web       search and embeddings — polite pacing, nothing more

Lanes are independent, so a play can fetch audio while the nightly job is still
transcribing something else. Within a lane, whoever is waited on by a person
goes first: priority comes from a context variable, so a request handler sets it
once and every service call underneath inherits it without threading an argument
through six signatures.

Serialisation crosses processes too, through a lock file per lane, because the
nightly script is a separate process and coordinating only within the app would
leave the original problem in place.
"""
import asyncio
import contextlib
import contextvars
import errno
import fcntl
import itertools
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Someone is waiting on a screen for this.
USER = 0
# They pressed a button and can watch it run.
INTERACTIVE = 1
# Housekeeping. Nobody is watching, so it yields to everything else.
BACKGROUND = 2

PRIORITY_NAMES = {USER: "user", INTERACTIVE: "interactive", BACKGROUND: "background"}

# Seconds to leave between turns in a lane. Only YouTube needs it: asking
# yt-dlp for metadata in a tight loop is what tripped its bot check during the
# channel work and had the address refused for everything afterwards.
SPACING = {
    "youtube": 4.0,
    "media": 0.0,
    "stt": 0.0,
    "llm": 0.0,
    "web": 0.0,
}

DEFAULT_SPACING = 0.0

# How long to wait for another process to give up a lane before going ahead
# anyway. A crashed process releases its lock when it exits, so this covers the
# one case the OS cannot: a lock leaked inside this process. Waiting forever
# there would wedge the app permanently, which is worse than two things
# overlapping once.
LOCK_WAIT_CEILING = 120.0

# The priority of work started without saying otherwise. Background, because
# every caller that a person is waiting on says so explicitly, and guessing
# wrong in that direction only makes housekeeping wait.
_priority: contextvars.ContextVar[int] = contextvars.ContextVar("job_priority", default=BACKGROUND)

# Lanes this task already holds. Taking a lane twice from the same task is a
# deadlock — the inner turn waits for an outer one that cannot finish until the
# inner one does — and nesting is natural here: fetching captions is one
# labelled turn that internally calls the metadata helper, which takes the same
# lane on its own behalf. So a re-entry is a no-op rather than a wait.
_held_by_task: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "held_lanes", default=frozenset()
)

_counter = itertools.count()


@dataclass(order=True)
class _Waiter:
    priority: int
    sequence: int
    label: str = field(compare=False)
    event: asyncio.Event = field(compare=False, default_factory=asyncio.Event)
    since: float = field(compare=False, default_factory=time.time)


@dataclass
class _Lane:
    name: str
    waiting: list[_Waiter] = field(default_factory=list)
    current: str | None = None
    current_since: float = 0.0
    current_priority: int = BACKGROUND
    last_finished: float = 0.0
    busy: bool = False
    done: int = 0


@dataclass
class Turn:
    """Handed to the body of a turn.

    `duplicate` says the same work was already in flight when this one asked —
    two browsers pressing play on the same episode, say. The waiting is done
    either way, so by the time a duplicate turn runs, the work it wanted has
    already happened and its body should do nothing.
    """
    duplicate: bool = False


_lanes: dict[str, _Lane] = {}
# Work in flight by key, so the same job asked for twice is done once.
_in_flight: dict[str, asyncio.Event] = {}
_lock_dir: Path | None = None


def _lane(name: str) -> _Lane:
    if name not in _lanes:
        _lanes[name] = _Lane(name=name)
    return _lanes[name]


def set_lock_dir(path) -> None:
    """Where the cross-process lock files live. Set once, at startup."""
    global _lock_dir
    _lock_dir = Path(path)
    try:
        _lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("cannot create the job lock directory %s: %s", path, exc)
        _lock_dir = None


@contextlib.contextmanager
def _cross_process(name: str):
    """Hold the lane's lock file for as long as the turn lasts.

    Advisory and best-effort: if the lock cannot be taken — no directory, a
    read-only filesystem — the work still runs. Serialising within this process
    is most of the value, and failing to do housekeeping because a lock file
    could not be made would be worse than doing it unserialised.
    """
    if _lock_dir is None:
        yield
        return
    path = _lock_dir / f"{name}.lock"
    handle = None
    try:
        handle = open(path, "w")
        deadline = time.time() + LOCK_WAIT_CEILING
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.time() > deadline:
                    log.warning(
                        "lane %s still locked after %.0fs; proceeding unserialised",
                        name, LOCK_WAIT_CEILING,
                    )
                    break
                # Another process — the nightly script — holds this lane.
                time.sleep(0.5)
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    except OSError as exc:
        log.warning("job lock %s unavailable (%s); running unserialised", name, exc)
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()


def priority() -> int:
    return _priority.get()


@contextlib.contextmanager
def priority_scope(level: int):
    """Everything started inside this block counts as `level`.

    A request handler sets it once and every service call underneath inherits
    it, which is why no service signature has to carry a priority argument.
    """
    token = _priority.set(level)
    try:
        yield
    finally:
        _priority.reset(token)


@contextlib.asynccontextmanager
async def lane(name: str, label: str = "", level: int | None = None, key: str | None = None):
    """Take a turn in `name`, waiting for whoever is ahead.

    Ordering among waiters is by priority, then by arrival: a listener's play
    overtakes the nightly backlog, but two jobs of equal standing keep their
    order rather than starving each other.

    `key` deduplicates. Pressing play from a second browser while the first
    download runs should not queue the same download again — with a key, the
    second caller waits for the first to finish and is then told it was a
    duplicate, so its body can skip work that has already been done.
    """
    if key is not None:
        existing = _in_flight.get(key)
        if existing is not None:
            await existing.wait()
            yield Turn(duplicate=True)
            return
    already = _held_by_task.get()
    if name in already:
        # Already inside a turn in this lane: nesting is not a second turn.
        yield Turn()
        return

    this = _lane(name)
    level = priority() if level is None else level
    waiter = _Waiter(priority=level, sequence=next(_counter), label=label or name)
    this.waiting.append(waiter)

    done_marker: asyncio.Event | None = None
    if key is not None:
        done_marker = asyncio.Event()
        _in_flight[key] = done_marker

    try:
        while True:
            if not this.busy:
                # Whoever is first in line goes; the rest keep waiting.
                nxt = min(this.waiting)
                if nxt is waiter:
                    break
            await _wait_briefly(waiter)

        this.waiting.remove(waiter)
        gap = SPACING.get(name, DEFAULT_SPACING) - (time.time() - this.last_finished)
        if gap > 0:
            await asyncio.sleep(gap)

        this.busy = True
        this.current = waiter.label
        this.current_since = time.time()
        this.current_priority = level
        held_token = _held_by_task.set(already | {name})
        try:
            # The cross-process lock is taken in a thread: flock blocks, and
            # blocking the event loop would stall every request in the app.
            await asyncio.get_event_loop().run_in_executor(None, _enter_lock, name)
            yield Turn()
        finally:
            _held_by_task.reset(held_token)
            await asyncio.get_event_loop().run_in_executor(None, _exit_lock, name)
            this.busy = False
            this.current = None
            this.last_finished = time.time()
            this.done += 1
            _wake(this)
    finally:
        if waiter in this.waiting:
            this.waiting.remove(waiter)
            _wake(this)
        if done_marker is not None:
            # Release anyone who asked for the same work, whether this turn
            # succeeded or raised: they must not wait on a job that has stopped.
            _in_flight.pop(key, None)
            done_marker.set()


# The cross-process lock is a context manager, but it has to be entered and left
# from a worker thread, so it is held open here between the two halves.
_held: dict[str, object] = {}


def _enter_lock(name: str) -> None:
    ctx = _cross_process(name)
    ctx.__enter__()
    _held[name] = ctx


def _exit_lock(name: str) -> None:
    ctx = _held.pop(name, None)
    if ctx is not None:
        with contextlib.suppress(Exception):
            ctx.__exit__(None, None, None)


async def _wait_briefly(waiter: _Waiter) -> None:
    """Sleep until woken, or briefly — whichever comes first.

    A timeout as well as an event because a lane can free up in another task's
    `finally`, and a waiter that missed the wake would otherwise sleep forever.
    """
    waiter.event.clear()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(waiter.event.wait(), timeout=0.25)


def _wake(this: _Lane) -> None:
    for waiter in this.waiting:
        waiter.event.set()


def status() -> dict:
    """What every lane is doing, for the screen that is waiting on it."""
    out = {}
    now = time.time()
    for name, this in sorted(_lanes.items()):
        out[name] = {
            "running": this.current,
            "running_for": round(now - this.current_since, 1) if this.current else 0,
            "priority": PRIORITY_NAMES.get(this.current_priority, "background"),
            "waiting": len(this.waiting),
            "queue": [w.label for w in sorted(this.waiting)][:5],
            "completed": this.done,
        }
    return out


def waiting_for(name: str) -> int:
    """How many turns are queued in a lane, for a caller reporting progress."""
    return len(_lane(name).waiting)


def in_flight() -> list[str]:
    """Keys currently being worked on, for tests and diagnostics."""
    return sorted(_in_flight)


def reset() -> None:
    """Forget every lane, releasing anything still held. Tests only.

    Releasing matters: dropping the map without closing the files would leave
    the descriptors flocked for the life of the process, and the next turn in
    that lane would wait on a lock nothing will ever give up.
    """
    for name in list(_held):
        _exit_lock(name)
    _lanes.clear()
    _held.clear()
    for event in _in_flight.values():
        event.set()
    _in_flight.clear()
