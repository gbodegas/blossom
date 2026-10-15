# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A Read press: its attempt, its calls in worker threads, its deadline and what it stores.

Every reader here is fake, a function standing for the call, except where the real SDK runs
on a mock transport, so nothing leaves the machine. A short deadline stands for the press's
120 seconds where a test waits it out; events, not sleeps, decide which call finishes when.
The images are synthetic bytes, and the answers are written in code for Wren.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from fastapi.testclient import TestClient

from blossom import screenshot_reading
from blossom.anthropic_client import ModelUnavailable, ScreenshotAnswer, read_screenshot
from blossom.app import create_app
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.screenshot_reading import PARALLEL_READS, ReadOutcome, read_screenshots
from blossom.screenshots import homework_rows
from blossom.settings import ANTHROPIC_API_KEY_VARIABLE, Settings
from blossom.stores import import_drafts
from blossom.stores.drafts import WriterBusy
from blossom.stores.import_drafts import (
    ImportDraftsStore,
    NewImage,
    new_creation_token,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    SAME_ORIGIN,
    SetClock,
    fixture_settings,
    homework_answer,
    trickling_answer,
)

T = TypeVar("T")
HOMEWORK = "homework_screenshot"
OBSERVED_AT = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
WITH_KEY = Settings.from_environment({ANTHROPIC_API_KEY_VARIABLE: "sk-test-value"})
ANSWERED = ScreenshotAnswer(homework_answer(), None, 1500, 300)


def image(label: str) -> NewImage:
    return NewImage(png=f"synthetic-png-{label}".encode(), width=800, height=600)


class Press:
    """One household file with her record and a homework draft store on the real monotonic
    clock, and a fake reader that answers each image as the test says."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0.0
        record = ProjectStateStore.open(path, SetClock(date(2026, 10, 9), OBSERVED_AT))
        self.student = record.student_id()
        record.close()
        self.store = self.reopen()
        self.answers: dict[bytes, list[ScreenshotAnswer | Exception]] = {}
        self.calls: list[tuple[bytes, float]] = []
        self.held: set[asyncio.Future[Any]] = set()
        self.gates: dict[bytes, threading.Event] = {}
        self.entered: dict[bytes, threading.Event] = {}
        self.lock = threading.Lock()
        self.running = 0
        self.most_running = 0
        self.pause = 0.0
        # The application's loop lives on after a press returns, and so does this one, so
        # a call a press left open runs on to its end as it would there.
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(30)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        self.store.close()

    def monotonic(self) -> float:
        """The real monotonic clock, moved on by hand past a deadline a test doesn't wait."""
        return time.monotonic() + self.offset

    def later(self, seconds: float) -> None:
        self.offset += seconds

    def reopen(self) -> ImportDraftsStore:
        return ImportDraftsStore.open(
            self.path,
            SetClock(date(2026, 10, 9), OBSERVED_AT),
            self.monotonic,
            rows={HOMEWORK: homework_rows},
        )

    def draft(self, *labels: str) -> str:
        made = self.store.create(
            self.student,
            kind=HOMEWORK,
            made_by="parent",
            creation_token=new_creation_token(),
            images=[image(label) for label in labels],
        )
        assert made is not None
        return made.draft_id

    def ids(self, draft_id: str) -> dict[str, str]:
        """Each image's id by its label."""
        snapshot = self.store.snapshot(self.student, draft_id)
        by_sha = {item.sha256: item.image_id for item in snapshot.images}
        return {
            label: by_sha[sha]
            for label, sha in (
                (label, hashlib.sha256(f"synthetic-png-{label}".encode()).hexdigest())
                for label in ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "x", "y")
            )
            if sha in by_sha
        }

    def answer(self, label: str, *answers: ScreenshotAnswer | Exception) -> None:
        self.answers[f"synthetic-png-{label}".encode()] = list(answers)

    def hold_open(self, label: str) -> tuple[threading.Event, threading.Event]:
        """Keep the call for ``label`` open until the returned gate is set; the other event
        is set once the call has begun."""
        png = f"synthetic-png-{label}".encode()
        self.gates[png] = threading.Event()
        self.entered[png] = threading.Event()
        return self.entered[png], self.gates[png]

    def reader(self, png: bytes, timeout: float) -> ScreenshotAnswer:
        with self.lock:
            self.calls.append((png, timeout))
            self.running += 1
            self.most_running = max(self.most_running, self.running)
        try:
            if png in self.entered:
                self.entered[png].set()
                self.gates[png].wait(10)
            time.sleep(self.pause)
            queued = self.answers.get(png, [])
            answer = queued.pop(0) if queued else ANSWERED
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            with self.lock:
                self.running -= 1

    def press(self, draft_id: str, *labels: str, settings: Settings = WITH_KEY) -> ReadOutcome:
        ids = self.ids(draft_id)
        return self.run(
            read_screenshots(
                self.store,
                settings,
                self.student,
                draft_id,
                [ids.get(label, label) for label in labels],
                monotonic=self.monotonic,
                held=self.held,
                reader=self.reader,
            )
        )

    def images(self, draft_id: str) -> dict[str, Any]:
        snapshot = self.store.snapshot(self.student, draft_id)
        labels = {image_id: label for label, image_id in self.ids(draft_id).items()}
        return {labels[item.image_id]: item for item in snapshot.images}


@pytest.fixture
def press(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Press]:
    monkeypatch.setattr(screenshot_reading, "RETRY_PAUSE_SECONDS", 0.0)
    made = Press(tmp_path / "blossom.sqlite3")
    yield made
    made.close()


def short_deadline(monkeypatch: pytest.MonkeyPatch, seconds: float = 0.5) -> None:
    """A press's deadline, made short so a test can wait it out."""
    monkeypatch.setattr(import_drafts, "READ_DEADLINE_SECONDS", seconds)


# ------------------------------------------------------------------ a press that reads


def test_a_press_reads_each_unread_image_it_names_and_stores_what_was_read(press: Press) -> None:
    draft_id = press.draft("a", "b")
    press.answer("b", ScreenshotAnswer(homework_answer(image_kind="unsupported"), None, 900, 40))

    outcome = press.press(draft_id, "a", "b")

    ids = press.ids(draft_id)
    assert sorted(outcome.read) == sorted([ids["a"], ids["b"]])
    assert (outcome.failed, outcome.dropped, outcome.busy, outcome.unfinished) == ((), (), (), ())
    images = press.images(draft_id)
    assert images["a"].read
    assert images["b"].read
    assert {png for png, _ in press.calls} == {b"synthetic-png-a", b"synthetic-png-b"}


def test_the_bytes_read_are_the_stored_prepared_bytes(press: Press) -> None:
    draft_id = press.draft("a")

    press.press(draft_id, "a")

    [(sent, _)] = press.calls
    assert sent == press.store.image_png(press.student, draft_id, press.ids(draft_id)["a"])


@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        (ScreenshotAnswer(None, "not_json", 10, 5), "not_json"),
        (ScreenshotAnswer(None, "cut_off", 10, 6000), "cut_off"),
        (ScreenshotAnswer(None, "model_refused", 10, 5), "model_refused"),
        (ScreenshotAnswer(None, "service_refused", 0, 0), "service_refused"),
        (ScreenshotAnswer({"image_kind": "email"}, None, 10, 5), "not_valid"),
    ],
)
def test_a_failed_read_is_stored_by_its_kind_with_no_second_call(
    press: Press, answer: ScreenshotAnswer, failure: str
) -> None:
    """No repair: an answer, valid or not, is never asked for again."""
    draft_id = press.draft("a")
    press.answer("a", answer)

    outcome = press.press(draft_id, "a")

    assert outcome.failed == ((press.ids(draft_id)["a"], failure),)
    assert len(press.calls) == 1
    assert press.images(draft_id)["a"].failure == failure


# ------------------------------------------------------------------ retries


def test_a_busy_service_is_asked_once_more_and_the_second_answer_stored(press: Press) -> None:
    draft_id = press.draft("a")
    press.answer("a", ScreenshotAnswer(None, "service_busy", 0, 0), ANSWERED)

    outcome = press.press(draft_id, "a")

    assert outcome.read == (press.ids(draft_id)["a"],)
    assert len(press.calls) == 2


def test_a_service_busy_twice_is_stored_as_busy_after_two_calls(press: Press) -> None:
    draft_id = press.draft("a")
    busy = ScreenshotAnswer(None, "service_busy", 0, 0)
    press.answer("a", busy, busy, ANSWERED)

    outcome = press.press(draft_id, "a")

    assert outcome.failed == ((press.ids(draft_id)["a"], "service_busy"),)
    assert len(press.calls) == 2


def test_with_more_than_90_seconds_left_a_call_is_given_90_seconds(press: Press) -> None:
    """The reader is handed a timeout of 90 seconds under the press's 120 second deadline. The
    argument is checked here; the trickling tests check time spent over a mock service."""
    draft_id = press.draft("a")

    press.press(draft_id, "a")

    [(_, timeout)] = press.calls
    assert timeout == screenshot_reading.CALL_TIMEOUT_SECONDS == 90.0


def test_with_less_than_90_seconds_left_a_call_is_given_only_what_is_left(
    press: Press, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a 5 second deadline the reader is handed a timeout of at most 5 seconds. The
    argument is checked here; the trickling tests check time spent over a mock service."""
    short_deadline(monkeypatch, 5.0)
    draft_id = press.draft("a")

    press.press(draft_id, "a")

    [(_, timeout)] = press.calls
    assert 0 < timeout <= 5.0


def test_a_busy_answer_after_the_deadline_is_not_asked_again(
    press: Press, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first call outlives the deadline and comes back busy: no retry, and the press has
    already returned with the image unfinished."""
    short_deadline(monkeypatch)
    draft_id = press.draft("a")
    press.answer("a", ScreenshotAnswer(None, "service_busy", 0, 0))
    entered, gate = press.hold_open("a")

    outcome = press.press(draft_id, "a")
    gate.set()
    finish_held(press)

    assert outcome.unfinished == (press.ids(draft_id)["a"],)
    assert len(press.calls) == 1
    assert entered.is_set()


def finish_held(press: Press) -> None:
    """Wait for the calls a press left running when it returned."""
    deadline = time.monotonic() + 10
    while press.held and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not press.held


# ------------------------------------------------------------------ the deadline


def test_the_press_returns_by_its_deadline_and_a_late_result_is_dropped(
    press: Press, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The call still open at the deadline is held until it ends; its result is dropped with
    its timing alone logged, and the image reads as not read in time."""
    short_deadline(monkeypatch)
    draft_id = press.draft("a", "b")
    named = homework_answer(active_student={"name": "Sentinel-N1", "uncertain": "Sentinel-U2"})
    press.answer("b", ScreenshotAnswer(named, None, 1500, 300))
    _, gate = press.hold_open("b")
    started = time.monotonic()

    with caplog.at_level(logging.DEBUG):
        outcome = press.press(draft_id, "a", "b")
        returned = time.monotonic() - started
        assert len(press.held) == 1
        gate.set()
        finish_held(press)

    ids = press.ids(draft_id)
    assert returned < 0.5 + 0.4
    assert (outcome.read, outcome.unfinished) == ((ids["a"],), (ids["b"],))
    images = press.images(draft_id)
    assert not images["b"].read
    assert images["b"].in_ended_attempt
    assert "a reading was dropped after" in caplog.text
    assert "Sentinel" not in caplog.text


def test_after_a_restart_an_image_left_unfinished_reads_as_not_read_yet(
    press: Press, monkeypatch: pytest.MonkeyPatch
) -> None:
    short_deadline(monkeypatch)
    draft_id = press.draft("a")
    _, gate = press.hold_open("a")
    press.press(draft_id, "a")
    gate.set()
    finish_held(press)
    assert press.images(draft_id)["a"].in_ended_attempt

    press.store.close()
    press.store = press.reopen()
    press.store.clear_attempts()

    image_a = press.images(draft_id)["a"]
    assert not image_a.in_ended_attempt
    assert not image_a.read
    press.gates.clear()
    press.entered.clear()
    assert press.press(draft_id, "a").read == (press.ids(draft_id)["a"],)


# ------------------------------------------------------------------ results the store drops


def test_a_late_result_never_restores_a_removed_image_and_never_attaches_to_its_re_add(
    press: Press,
) -> None:
    """Tab A's call for X is held open; tab B removes X, and adds the same screenshot again.
    The call then returns before the deadline, and its result is dropped."""
    draft_id = press.draft("a", "x")
    entered, gate = press.hold_open("x")
    removed_id = press.ids(draft_id)["x"]

    async def two_tabs() -> ReadOutcome:
        pressing = asyncio.ensure_future(
            read_screenshots(
                press.store,
                WITH_KEY,
                press.student,
                draft_id,
                list(press.ids(draft_id).values()),
                monotonic=press.monotonic,
                held=press.held,
                reader=press.reader,
            )
        )
        await asyncio.to_thread(entered.wait, 10)
        await asyncio.to_thread(press.store.remove_image, press.student, draft_id, removed_id)
        await asyncio.to_thread(press.store.add_images, press.student, draft_id, [image("x")])
        gate.set()
        return await pressing

    outcome = press.run(two_tabs())

    readded = press.ids(draft_id)["x"]
    assert readded != removed_id
    assert outcome.dropped == (removed_id,)
    again = press.images(draft_id)["x"]
    assert (again.read, again.covered, again.failure) == (False, False, None)


def test_an_image_removed_before_its_call_starts_is_not_read(press: Press) -> None:
    draft_id = press.draft("a", "b")
    entered, gate = press.hold_open("a")
    ids = press.ids(draft_id)

    async def remove_first() -> ReadOutcome:
        pressing = asyncio.ensure_future(
            read_screenshots(
                press.store,
                WITH_KEY,
                press.student,
                draft_id,
                [ids["a"], ids["b"]],
                monotonic=press.monotonic,
                held=press.held,
                reader=press.reader,
                parallel=1,
            )
        )
        await asyncio.to_thread(entered.wait, 10)
        await asyncio.to_thread(press.store.remove_image, press.student, draft_id, ids["b"])
        gate.set()
        return await pressing

    outcome = press.run(remove_first())

    assert outcome.dropped == (ids["b"],)
    assert [png for png, _ in press.calls] == [b"synthetic-png-a"]


class BusyFile:
    """The draft store, but its file's writer stays held by another connection when a result
    comes to be stored."""

    def __init__(self, store: ImportDraftsStore) -> None:
        self.store = store

    def __getattr__(self, name: str) -> Callable[..., Any]:
        return getattr(self.store, name)  # type: ignore[no-any-return]

    def store_reading(self, *args: object, **kwargs: object) -> bool:
        raise WriterBusy

    def store_failure(self, *args: object, **kwargs: object) -> bool:
        raise WriterBusy


def test_a_busy_file_leaves_the_image_unread_and_names_it(press: Press) -> None:
    draft_id = press.draft("a")
    press.store = BusyFile(press.store)  # type: ignore[assignment]

    outcome = press.press(draft_id, "a")

    assert outcome.busy == (press.ids(draft_id)["a"],)
    assert outcome.read == ()
    assert not press.images(draft_id)["a"].read


# ------------------------------------------------------------------ what a press starts


def test_a_second_press_during_a_current_attempt_makes_no_call(press: Press) -> None:
    draft_id = press.draft("a")
    entered, gate = press.hold_open("a")

    async def twice() -> tuple[ReadOutcome, ReadOutcome]:
        first = asyncio.ensure_future(
            read_screenshots(
                press.store,
                WITH_KEY,
                press.student,
                draft_id,
                list(press.ids(draft_id).values()),
                monotonic=press.monotonic,
                held=press.held,
                reader=press.reader,
            )
        )
        await asyncio.to_thread(entered.wait, 10)
        second = await read_screenshots(
            press.store,
            WITH_KEY,
            press.student,
            draft_id,
            list(press.ids(draft_id).values()),
            monotonic=press.monotonic,
            held=press.held,
            reader=press.reader,
        )
        gate.set()
        return await first, second

    first, second = press.run(twice())

    assert first.read == (press.ids(draft_id)["a"],)
    assert second.under_way_until is not None
    assert not second.started
    assert len(press.calls) == 1


def test_a_press_reads_only_the_images_its_page_named(press: Press) -> None:
    """Y, added by another tab after this page was made, isn't read; an id the draft doesn't
    hold is passed over, and a press naming only such ids reads nothing."""
    draft_id = press.draft("x")
    press.store.add_images(press.student, draft_id, [image("y")])

    outcome = press.press(draft_id, "x")
    tampered = press.press(draft_id, "not-an-image-of-this-draft")

    assert outcome.read == (press.ids(draft_id)["x"],)
    assert not press.images(draft_id)["y"].read
    assert not tampered.started
    assert [png for png, _ in press.calls] == [b"synthetic-png-x"]


def test_a_press_reads_unread_and_failed_images_and_never_replaces_a_reading(
    press: Press,
) -> None:
    draft_id = press.draft("a", "b")
    press.answer("b", ScreenshotAnswer(None, "not_json", 10, 5))
    press.press(draft_id, "a", "b")
    press.calls.clear()
    press.later(import_drafts.READ_DEADLINE_SECONDS)

    outcome = press.press(draft_id, "a", "b")

    assert outcome.read == (press.ids(draft_id)["b"],)
    assert [png for png, _ in press.calls] == [b"synthetic-png-b"]


def test_with_no_key_no_attempt_starts_and_nothing_is_called(press: Press) -> None:
    draft_id = press.draft("a")

    with pytest.raises(ModelUnavailable):
        press.press(draft_id, "a", settings=Settings.from_environment({}))

    assert press.calls == []
    assert not press.images(draft_id)["a"].covered


def test_calls_run_at_most_the_chosen_number_at_once(press: Press) -> None:
    """Ten images, each call held a moment: never more than the chosen number run at once,
    and all ten are read."""
    labels = ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j")
    draft_id = press.draft(*labels)
    press.pause = 0.05

    outcome = press.press(draft_id, *labels)

    assert len(outcome.read) == 10
    assert 1 < press.most_running <= PARALLEL_READS == 5


# ------------------------------------------------------------------ inside the app


def test_a_reading_inside_the_app_adds_nothing_to_the_trace_store_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real SDK on a mock transport, the app's own file and trace store: the reading is
    stored, the trace store gains no row, and no sentinel reaches the log."""
    named = homework_answer(active_student={"name": "Sentinel-N3", "uncertain": "Sentinel-U4"})
    sent: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_synthetic",
                "type": "message",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(named)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1500, "output_tokens": 300},
            },
        )

    settings = fixture_settings(**{ANTHROPIC_API_KEY_VARIABLE: "sk-test-value"})
    transport = httpx.MockTransport(service)
    app = create_app(settings)
    with TestClient(app, headers=SAME_ORIGIN), caplog.at_level(logging.DEBUG):
        state: ApplicationState = getattr(app.state, STATE_ATTRIBUTE)
        before = trace_rows(state.settings.trace_path)
        store = ImportDraftsStore.open(
            state.settings.database_path,
            state.real_clock,
            state.monotonic,
            rows={HOMEWORK: homework_rows},
        )
        student = store_student(state)
        made = store.create(
            student,
            kind=HOMEWORK,
            made_by="household",
            creation_token=new_creation_token(),
            images=[image("a")],
        )
        assert made is not None
        [only] = store.snapshot(student, made.draft_id).images
        outcome = asyncio.run(
            read_screenshots(
                store,
                state.settings,
                student,
                made.draft_id,
                [only.image_id],
                monotonic=state.monotonic,
                held=set(),
                reader=lambda png, timeout: read_screenshot(
                    state.settings, png, timeout=timeout, transport=transport
                ),
            )
        )
        after = trace_rows(state.settings.trace_path)
        store.close()

    assert outcome.read == (only.image_id,)
    assert len(sent) == 1
    assert after == before
    assert "Sentinel" not in caplog.text


def trace_rows(path: Path) -> dict[str, int]:
    """Every table of the trace file with its row count."""
    connection = sqlite3.connect(path)
    try:
        names = [
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            name: int(connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])  # noqa: S608
            for name in names
        }
    finally:
        connection.close()


def store_student(state: ApplicationState) -> str:
    return state.project_state.student_id()


def test_a_finished_press_holds_nothing(press: Press) -> None:
    draft_id = press.draft("a")

    press.press(draft_id, "a")

    assert press.held == set()


def test_the_outcome_holds_ids_and_kinds_alone(press: Press) -> None:
    draft_id = press.draft("a", "b")
    named = homework_answer(active_student={"name": "Sentinel-N5", "uncertain": None})
    press.answer("a", ScreenshotAnswer(named, None, 10, 5))
    press.answer("b", ScreenshotAnswer({"Sentinel-K6": "Sentinel-V7"}, None, 10, 5))

    outcome = press.press(draft_id, "a", "b")

    assert "Sentinel" not in repr(outcome)
    assert outcome.failed == ((press.ids(draft_id)["b"], "not_valid"),)


def test_a_reader_that_raises_ends_unfinished_and_is_logged_by_its_kind_alone(
    press: Press, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An error nothing expects, raised before the deadline and after it, each carrying text:
    the press returns by its deadline, both images are unfinished, and the log names each
    error's kind alone, with no traceback."""
    short_deadline(monkeypatch)
    draft_id = press.draft("a", "b", "c")
    outer = RuntimeError("Sentinel-outer-O2")
    outer.__cause__ = ValueError("Sentinel-inner-I1 synthetic-png-a")
    press.answer("a", outer)
    press.answer("b", KeyError("Sentinel-late-L3"))
    _, gate = press.hold_open("b")
    started = time.monotonic()

    with caplog.at_level(logging.DEBUG):
        outcome = press.press(draft_id, "a", "b", "c")
        returned = time.monotonic() - started
        gate.set()
        finish_held(press)

    ids = press.ids(draft_id)
    assert returned < 0.5 + 0.4
    assert outcome.read == (ids["c"],)
    assert sorted(outcome.unfinished) == sorted([ids["a"], ids["b"]])
    ended = [r for r in caplog.records if r.getMessage().startswith("a screenshot reading ended")]
    assert sorted(record.getMessage() for record in ended) == [
        "a screenshot reading ended with KeyError",
        "a screenshot reading ended with RuntimeError",
    ]
    assert all(record.levelno == logging.ERROR for record in ended)
    assert not [record for record in caplog.records if record.exc_info]
    assert not [record for record in caplog.records if record.name == "asyncio"]
    assert "Sentinel" not in caplog.text


# ------------------------------------------------------------------ calls that trickle

TRICKLE_SECONDS = 3.0
"""How long each trickling answer would take to arrive in full."""

HELD_POOL_THREADS = 16
"""The held-workers test's thread pool: fewer than its twenty calls, and the default size on a
twelve-core machine."""


def trickling_reader(
    seconds: float = TRICKLE_SECONDS,
) -> Callable[[bytes, float], ScreenshotAnswer]:
    """The real reader call, on a mock service whose answer trickles in."""
    transport = trickling_answer(homework_answer(), seconds)
    return lambda png, timeout: read_screenshot(WITH_KEY, png, timeout=timeout, transport=transport)


def test_a_stop_waits_on_no_trickling_call_past_its_time_plus_one_read(
    press: Press, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``asyncio.run`` waits for the executor's threads when it ends, as the server's stop does:
    a press over trickling answers lets it end by the deadline and one more piece."""
    short_deadline(monkeypatch)
    draft_id = press.draft("a", "b", "c", "d", "e")
    ids = list(press.ids(draft_id).values())
    started = time.monotonic()

    outcome = asyncio.run(
        read_screenshots(
            press.store,
            WITH_KEY,
            press.student,
            draft_id,
            ids,
            monotonic=time.monotonic,
            held=set(),
            reader=trickling_reader(),
        )
    )

    assert time.monotonic() - started < 0.5 + 0.5
    assert sorted(outcome.unfinished) == sorted(ids)


def test_with_every_worker_held_by_trickling_calls_later_reads_still_run(
    press: Press, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four presses of five trickling calls want twenty threads from a pool of sixteen, set on
    the test loop so the test doesn't depend on the CPU count. Once their deadline passes, a
    press on another draft and the planner's kind of threaded work each run within a second."""
    short_deadline(monkeypatch)
    labels = ("a", "b", "c", "d", "e")
    held = [press.draft(*labels) for _ in range(4)]
    other = press.draft("x")

    async def press_on(
        draft_id: str, reader: Callable[[bytes, float], ScreenshotAnswer]
    ) -> ReadOutcome:
        return await read_screenshots(
            press.store,
            WITH_KEY,
            press.student,
            draft_id,
            list(press.ids(draft_id).values()),
            monotonic=time.monotonic,
            held=press.held,
            reader=reader,
        )

    async def then_more() -> tuple[str, ReadOutcome, float]:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=HELD_POOL_THREADS)
        )
        await asyncio.gather(*(press_on(draft_id, trickling_reader()) for draft_id in held))
        await asyncio.sleep(0.15)
        planned = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, lambda: "planned"), 1.0
        )
        started = time.monotonic()
        later = await press_on(other, press.reader)
        return planned, later, time.monotonic() - started

    planned, later, took = press.run(then_more())
    finish_held(press)

    assert planned == "planned"
    assert later.read == (press.ids(other)["x"],)
    assert took < 1.0
