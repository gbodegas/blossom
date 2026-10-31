# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A decision pressed on the family page when a store fails: what the answer may say.

Before the graph's write begins, a failed read answers that the record couldn't be read, and
nothing was written. Once the write has begun and raised, the record is read once more, after
the write has ended and under the same lock. A decision the table records consistently is
said to be recorded, as this press's only when the press resumed nothing and its choice and
words are the ones recorded. Every other row, and a record that can't be read again, answers
that it can't be told: a decision the thread holds can still be recorded later, by the sweep,
a publish or the next press. Words typed for the plan are kept, read-only, wherever they
aren't known to be stored. The JSON route answers as it did. The fixture week through the
app, a pinned clock, and a failure made at the store call, or a second connection holding
the file.
"""

import asyncio
import contextlib
import logging
import pathlib
import sqlite3
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.agent.retention import sweep_saved_state
from blossom.app import create_app
from blossom.drafts import DraftStatus
from blossom.routes import parent as parent_routes
from blossom.routes.runs import plan_graphs
from blossom.stores.drafts import DraftRecord, StoreBusy, WriterBusy
from tests.support import (
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Statements,
    accepting,
    after_the_failure,
    browser,
    files_in,
    fixture_week_plan,
    main_of,
    plan_form,
    planned,
    scripted_graphs,
    signed_in,
    signed_in_household,
    state_of,
    store_free_page,
    walkthrough,
    ways_back_of,
    words,
)

DECIDE = "/parent/actions/decide/{}"
APPROVALS = "/parent/approvals/{}"
APPROVED = "Your decision was recorded: this plan is approved."
ASKED_FOR_A_CHANGE = "Your decision was recorded: you asked for a change to this plan."
A_DECISION = "A decision on this plan is recorded."
UNKNOWN = "Blossom couldn't tell whether a decision on this plan was recorded."
TOO_SLOW = "The record couldn't be read in time. Try again in a moment."
RECORDS_UNREAD = "Blossom couldn't read the household's records."
FAMILY = "Family review can't be shown right now."
AS_TYPED = "Your note, as typed"
NOTE = "Start with <b>Geometry</b> & take a break"
NEVER = ("wasn't recorded", "Try again", "safe", "will be recorded", "Pressing")
"""Words no answer after the write began may hold: no refusal, no invitation to press again,
and no promise that a decision will land."""
BUTTONS = {"approve": ("approved", APPROVED), "refuse": ("rejected", ASKED_FOR_A_CHANGE)}
"""Each button's decision as the table records it, and the sentence for it once recorded."""
STATUS_OF = {
    "approved": DraftStatus.APPROVED_FOR_MANUAL_SEND,
    "rejected": DraftStatus.DRAFT,
}
DECIDED_AT = datetime(2026, 8, 19, 18, 0, tzinfo=UTC)


class Counted:
    """A store call counted, with what chosen calls do instead, by their place in order."""

    def __init__(
        self,
        real: Callable[..., Any],
        instead: dict[int, Callable[..., Any]] | None = None,
    ) -> None:
        self.real = real
        self.instead = instead or {}
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls += 1
        if self.calls in self.instead:
            return self.instead[self.calls](*args, **kwargs)
        return self.real(*args, **kwargs)


class CountedAsync(Counted):
    """``Counted`` for a coroutine method of the checkpoint saver."""

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls += 1
        if self.calls in self.instead:
            return await self.instead[self.calls](*args, **kwargs)
        return await self.real(*args, **kwargs)


def raising(error: BaseException) -> Callable[..., Any]:
    """A call that raises ``error``, the same object each time."""

    def raised(*_: object, **__: object) -> Any:  # noqa: ANN401
        raise error

    return raised


def raising_async(error: BaseException) -> Callable[..., Any]:
    """A coroutine call that raises ``error``."""

    async def raised(*_: object, **__: object) -> Any:  # noqa: ANN401
        raise error

    return raised


def coded(name: str) -> sqlite3.OperationalError:
    """An ``OperationalError`` as SQLite raises one with the code ``name`` names, with the
    message a busy file gives whatever the code."""
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = getattr(sqlite3, name)
    error.sqlite_errorname = name
    return error


@contextlib.contextmanager
def a_waiting_plan(**environ: str) -> Iterator[tuple[TestClient, str]]:
    """Today's plan, published and waiting at the gate, and the client that made it."""
    with browser(key=True, **environ) as client:
        walkthrough(client)
        yield client, planned(client).draft_id


def press(client: TestClient, draft_id: str, decision: str = "approve", reason: str = NOTE) -> Any:  # noqa: ANN401
    """The two buttons' form, sent as the family page sends it."""
    return client.post(
        DECIDE.format(draft_id),
        data={"decision": decision, "reason": reason},
        headers=PAGE_HEADERS,
    )


def row_of(client: TestClient, draft_id: str) -> tuple[object, object]:
    """The decision and the words the table holds for the draft."""
    record = state_of(client).drafts.get(draft_id)
    assert record is not None
    return record.decision, record.reason


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the family routes warned about, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == parent_routes.__name__ and record.levelno == logging.WARNING
    ]


def the_stand_in(answer: Any, *, status: int, said: str, note: str | None) -> str:  # noqa: ANN401
    """The family page's stand-in with ``said`` and the line that the page can't be shown, the
    note as typed read-only when ``note`` is given and no note otherwise, the way back, and
    none of the words no answer after the write may hold. Its main part."""
    main = store_free_page(answer, status=status, heading="Family review", alert=f"{said} {FAMILY}")
    if note is None:
        assert "<textarea" not in main
    else:
        assert f'<label for="kept-reason">{AS_TYPED}</label>' in main
        assert f"readonly>{escape(note)}</textarea>" in main
    assert "Note about this plan" not in main
    assert ways_back_of(main) == [("/parent", "Return to Family review")]
    for never in NEVER:
        assert never not in words(main), never
    return main


def reported(error: BaseException) -> str:
    """The error as the server's report shows it, with every cause and context it prints."""
    return "".join(traceback.format_exception(error))


def a_record(real: DraftRecord, **changed: object) -> DraftRecord:
    """The draft's record as the reread might find it, ``changed`` over what it is."""
    return real.model_copy(update=changed)


def recorded(real: DraftRecord, decision: str, reason: str | None) -> DraftRecord:
    """A consistent record of ``decision`` and ``reason``: its status, published, stamped."""
    return a_record(
        real, decision=decision, reason=reason, status=STATUS_OF[decision], decided_at=DECIDED_AT
    )


def sweep(client: TestClient) -> None:
    """One pass of the hourly sweep, on the app's own loop."""
    state = state_of(client)
    client.portal.call(sweep_saved_state, state.checkpointer, state.drafts, state.clock)  # type: ignore[union-attr]


# --------------------------------------------------------- before the write begins


@pytest.mark.parametrize(
    ("where", "error", "said"),
    [
        ("the record", StoreBusy("database is locked"), TOO_SLOW),
        ("the record", coded("SQLITE_IOERR"), RECORDS_UNREAD),
        ("the record", coded("SQLITE_BUSY"), TOO_SLOW),
        ("whether the plan still fits", coded("SQLITE_IOERR"), RECORDS_UNREAD),
    ],
    ids=["busy record", "unreadable record", "busy-coded record", "unreadable fit"],
)
def test_a_read_before_the_write_that_fails_says_so_and_writes_nothing(
    where: str,
    error: BaseException,
    said: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read the decision makes before its write, refused: 503 on the family page with the
    note back in its box, no second read of the record, and nothing written."""
    caplog.set_level(logging.WARNING)
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        gets = Counted(state.drafts.get)
        if where == "the record":
            gets.instead[1] = raising(error)
        else:
            fits = Counted(parent_routes.stale_reason, {1: raising(error)})
            monkeypatch.setattr(parent_routes, "stale_reason", fits)
        monkeypatch.setattr(state.drafts, "get", gets)
        answer = press(client, draft_id)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    assert answer.status_code == 503, answer.text[:600]
    assert said in words(main_of(answer.text))
    assert f'value="{escape(NOTE)}"' in answer.text
    assert gets.calls == 1
    assert after == (None, None)
    assert [type(error).__name__ in line for line in warnings_of(caplog)] == [True]


@pytest.mark.parametrize(
    ("code", "said"), [("SQLITE_IOERR", RECORDS_UNREAD), ("SQLITE_BUSY", TOO_SLOW)]
)
def test_the_saved_review_step_that_cant_be_read_says_the_record_cant_be_read(
    code: str, said: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread's saved state read before the write, refused: busy, the record couldn't be
    read in time, and otherwise the records sentence, 503, nothing written, and the record
    read once."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        gets = Counted(state.drafts.get)
        saved = CountedAsync(state.checkpointer.aget_tuple, {1: raising_async(coded(code))})
        monkeypatch.setattr(state.drafts, "get", gets)
        monkeypatch.setattr(state.checkpointer, "aget_tuple", saved)
        answer = press(client, draft_id)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    assert answer.status_code == 503, answer.text[:600]
    assert said in words(main_of(answer.text))
    assert gets.calls == 1
    assert after == (None, None)


def test_a_read_that_runs_past_the_wait_before_the_write_keeps_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing answer for a first read that ran past the store's wait stays: 503, the
    record couldn't be read in time, nothing written."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        monkeypatch.setattr(parent_routes, "STORE_WAIT_SECONDS", 0.2)
        gets = Counted(state.drafts.get, {1: lambda *a, **k: time.sleep(1.0)})
        monkeypatch.setattr(state.drafts, "get", gets)
        answer = press(client, draft_id)
        monkeypatch.undo()
        time.sleep(1.0)
        after = row_of(client, draft_id)

    assert answer.status_code == 503, answer.text[:600]
    assert TOO_SLOW in words(main_of(answer.text))
    assert after == (None, None)


# ------------------------------------------- the write raised; the record read again

WRITE_FAILURES: dict[str, Callable[[], BaseException]] = {
    "busy store": lambda: StoreBusy("database is locked"),
    "busy writer": lambda: WriterBusy("database is locked"),
    "busy commit": lambda: coded("SQLITE_BUSY"),
    "unreadable write": lambda: coded("SQLITE_IOERR"),
}


@pytest.mark.parametrize("button", ["approve", "refuse"])
@pytest.mark.parametrize("failure", list(WRITE_FAILURES))
def test_a_write_that_raised_with_the_row_still_waiting_cant_be_told(
    failure: str,
    button: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The decision's write refused, the row read again and still waiting: the thread holds the
    decision, so the answer can't say whether it will be recorded. 500 on the stand-in, the
    note kept read-only, the row left waiting, and one warning by the failure's kind. A sweep
    then records this press's decision, which is why the answer can't say it wasn't."""
    caplog.set_level(logging.WARNING)
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        gets = Counted(state.drafts.get)
        monkeypatch.setattr(state.drafts, "get", gets)
        monkeypatch.setattr(state.drafts, "record_decision", raising(WRITE_FAILURES[failure]()))
        answer = press(client, draft_id, button)
        monkeypatch.undo()
        waiting = row_of(client, draft_id)
        sweep(client)
        swept = row_of(client, draft_id)

    the_stand_in(answer, status=500, said=UNKNOWN, note=NOTE)
    assert gets.calls == 2
    assert waiting == (None, None)
    assert swept == (BUTTONS[button][0], NOTE)
    warned = warnings_of(caplog)
    assert len(warned) == 1
    assert type(WRITE_FAILURES[failure]()).__name__ in warned[0]
    assert "database is locked" not in warned[0]


@pytest.mark.parametrize("button", ["approve", "refuse"])
def test_a_write_that_raised_after_its_commit_is_said_to_be_recorded(
    button: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write committed and then raised: the record read again holds this press's choice and
    words, so the answer says this decision was recorded. 503 on the stand-in, no note, since
    it is stored, and no read after the reread."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        real = state.drafts.record_decision

        def committed_then_failed(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            real(*args, **kwargs)
            refused = coded("SQLITE_IOERR")
            raise refused

        snapshots = Counted(state.drafts.review_snapshot)
        gets = Counted(state.drafts.get)
        monkeypatch.setattr(state.drafts, "record_decision", committed_then_failed)
        monkeypatch.setattr(state.drafts, "review_snapshot", snapshots)
        monkeypatch.setattr(state.drafts, "get", gets)
        answer = press(client, draft_id, button)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    the_stand_in(answer, status=503, said=BUTTONS[button][1], note=None)
    assert after == (BUTTONS[button][0], NOTE)
    assert gets.calls == 2
    assert snapshots.calls == 0


@pytest.mark.parametrize(
    ("found", "said", "note"),
    [
        ("this press's", APPROVED, None),
        ("another reason", A_DECISION, NOTE),
        ("no reason", A_DECISION, NOTE),
        ("the other button's", A_DECISION, NOTE),
    ],
)
def test_a_decision_found_on_the_reread_is_this_presss_only_when_it_matches(
    found: str, said: str, note: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write raised, and the record read again holds a decision: this press's choice and
    words are said as this press's; another reason, or the other button's choice, only as a
    decision recorded, with the note kept as typed."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        real = state.drafts.get(draft_id)
        assert real is not None
        reread = {
            "this press's": recorded(real, "approved", NOTE),
            "another reason": recorded(real, "approved", "other words"),
            "no reason": recorded(real, "approved", None),
            "the other button's": recorded(real, "rejected", NOTE),
        }[found]
        gets = Counted(state.drafts.get, {2: lambda *a, **k: reread})
        monkeypatch.setattr(state.drafts, "get", gets)
        monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        answer = press(client, draft_id)

    the_stand_in(answer, status=503, said=said, note=note)


@pytest.mark.parametrize(
    "found",
    [
        "waiting",
        "expired",
        "superseded",
        "missing",
        "approved with a draft's status",
        "rejected with an approval's status",
        "approved but not published",
        "approved with no time",
    ],
)
def test_a_reread_that_finds_no_consistent_decision_cant_be_told(
    found: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write raised, and the record read again is waiting, closed another way, gone, or
    holds a decision its other columns don't bear out: none of them is taken as recorded or as
    refused. 500, the note kept."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        real = state.drafts.get(draft_id)
        assert real is not None
        reread = {
            "waiting": real,
            "expired": a_record(real, decision="expired", decided_at=DECIDED_AT),
            "superseded": a_record(real, decision="superseded", decided_at=DECIDED_AT),
            "missing": None,
            "approved with a draft's status": a_record(
                recorded(real, "approved", NOTE), status=DraftStatus.DRAFT
            ),
            "rejected with an approval's status": a_record(
                recorded(real, "rejected", NOTE), status=DraftStatus.APPROVED_FOR_MANUAL_SEND
            ),
            "approved but not published": a_record(
                recorded(real, "approved", NOTE), published=False
            ),
            "approved with no time": a_record(recorded(real, "approved", NOTE), decided_at=None),
        }[found]
        gets = Counted(state.drafts.get, {2: lambda *a, **k: reread})
        monkeypatch.setattr(state.drafts, "get", gets)
        monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        answer = press(client, draft_id)

    the_stand_in(answer, status=500, said=UNKNOWN, note=NOTE)


def test_with_no_note_typed_the_answer_that_cant_tell_shows_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an empty note, the answer that can't tell shows no note box."""
    with a_waiting_plan() as (client, draft_id):
        monkeypatch.setattr(state_of(client).drafts, "record_decision", raising(StoreBusy()))
        answer = press(client, draft_id, reason="")

    the_stand_in(answer, status=500, said=UNKNOWN, note=None)


# --------------------------------------------------------- the reread's own failures


@pytest.mark.parametrize(
    "reread",
    ["busy", "busy-coded", "unreadable", "past the wait"],
)
def test_a_reread_that_fails_cant_be_told_and_keeps_the_writes_own_failure(
    reread: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The write raised, and the record can't be read again: 500, the answer that can't tell,
    never the record-too-slow words or an invitation to try again, and the one warning names
    the write's own failure first."""
    caplog.set_level(logging.WARNING)
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        instead: dict[int, Callable[..., Any]] = {
            2: {
                "busy": raising(StoreBusy()),
                "busy-coded": raising(coded("SQLITE_BUSY")),
                "unreadable": raising(coded("SQLITE_IOERR")),
                "past the wait": lambda *a, **k: time.sleep(1.0),
            }[reread]
        }
        if reread == "past the wait":
            monkeypatch.setattr(parent_routes, "STORE_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(state.drafts, "get", Counted(state.drafts.get, instead))
        monkeypatch.setattr(state.drafts, "record_decision", raising(WriterBusy()))
        answer = press(client, draft_id)
        monkeypatch.undo()
        time.sleep(1.0 if reread == "past the wait" else 0.0)

    the_stand_in(answer, status=500, said=UNKNOWN, note=NOTE)
    assert TOO_SLOW not in answer.text
    warned = warnings_of(caplog)
    assert len(warned) == 1
    assert "WriterBusy" in warned[0]


@pytest.mark.parametrize("kind", [ValueError, RuntimeError])
def test_a_reread_that_fails_another_way_is_the_servers_error_after_the_write(
    kind: type[Exception], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The write raised, and reading the record again fails in a way no store fails: that is
    the server's error, carrying the write's own failure as its context, and no answer about
    the decision is made."""
    caplog.set_level(logging.WARNING)
    original = StoreBusy("database is locked")
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        gets = Counted(state.drafts.get, {2: raising(kind("not a store's failure"))})
        monkeypatch.setattr(state.drafts, "get", gets)
        monkeypatch.setattr(state.drafts, "record_decision", raising(original))
        with pytest.raises(kind) as caught:
            press(client, draft_id)

    report = reported(caught.value)
    assert "not a store's failure" in report
    assert "blossom.stores.drafts.StoreBusy: database is locked" in report
    assert warnings_of(caplog) == []


# ---------------------------------------- the write returned; the read after it fails


@pytest.mark.parametrize("button", ["approve", "refuse"])
@pytest.mark.parametrize("failure", ["busy", "unreadable", "past the wait"])
def test_a_decision_recorded_whose_record_cant_be_read_back_is_said_to_be_recorded(
    failure: str, button: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write returned, so this press's choice and words are recorded, and the read after it
    fails: the answer says so, 503, with no invitation to press again and no read after."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        if failure == "past the wait":
            monkeypatch.setattr(parent_routes, "STORE_WAIT_SECONDS", 0.2)
        late = {
            "busy": raising(StoreBusy()),
            "unreadable": raising(coded("SQLITE_IOERR")),
            "past the wait": lambda *a, **k: time.sleep(1.0),
        }[failure]
        gets = Counted(state.drafts.get, {2: late})
        snapshots = Counted(state.drafts.review_snapshot)
        monkeypatch.setattr(state.drafts, "get", gets)
        monkeypatch.setattr(state.drafts, "review_snapshot", snapshots)
        answer = press(client, draft_id, button)
        monkeypatch.undo()
        time.sleep(1.0 if failure == "past the wait" else 0.0)
        after = row_of(client, draft_id)

    the_stand_in(answer, status=503, said=BUTTONS[button][1], note=None)
    assert after == (BUTTONS[button][0], NOTE)
    assert gets.calls == 2
    assert snapshots.calls == 0


def test_a_resumed_decision_recorded_whose_record_cant_be_read_back_is_not_this_presss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first press's write refused leaves its decision in the thread; the next press finishes
    that decision, and when its record can't be read back the answer says a decision is
    recorded, never this press's, and keeps the note as typed."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        first = press(client, draft_id, "approve", NOTE)
        monkeypatch.undo()
        gets = Counted(state.drafts.get, {2: raising(StoreBusy())})
        monkeypatch.setattr(state.drafts, "get", gets)
        second = press(client, draft_id, "approve", NOTE)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    the_stand_in(first, status=500, said=UNKNOWN, note=NOTE)
    the_stand_in(second, status=503, said=A_DECISION, note=NOTE)
    assert after == ("approved", NOTE)


def test_a_resumed_decision_whose_write_raises_again_cant_be_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second press resumes the decision the thread holds, its write refused again and the
    row still waiting: it can't be told, whatever button was pressed."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        press(client, draft_id, "approve", NOTE)
        second = press(client, draft_id, "refuse", "other words")
        monkeypatch.undo()
        after = row_of(client, draft_id)

    the_stand_in(second, status=500, said=UNKNOWN, note="other words")
    assert after == (None, None)


def test_a_resumed_decision_that_lands_and_then_raises_is_not_this_presss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second press, the same button and the same words, resumes the decision the thread
    holds, and its write lands and then raises: the record read again holds that choice and
    those words, yet the decision is the first press's, so the answer says a decision is
    recorded, never this press's, and keeps the note as typed."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        real = state.drafts.record_decision
        monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        press(client, draft_id, "approve", NOTE)

        def landed_then_failed(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            real(*args, **kwargs)
            refused = coded("SQLITE_IOERR")
            raise refused

        monkeypatch.setattr(state.drafts, "record_decision", landed_then_failed)
        second = press(client, draft_id, "approve", NOTE)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    the_stand_in(second, status=503, said=A_DECISION, note=NOTE)
    assert after == ("approved", NOTE)


# ---------------------------------------------------- the checkpoint file's failures


def failing_checkpoint_writes(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, *, channels: list[str]
) -> None:
    """The saver's first write of exactly ``channels`` refused, busy-coded."""
    saver = state_of(client).checkpointer
    real = saver.aput_writes
    failed: list[bool] = []

    async def writes(config: Any, sent: Any, task_id: str, task_path: str = "") -> None:  # noqa: ANN401
        if not failed and [str(channel) for channel, _ in sent] == channels:
            failed.append(True)
            refused = coded("SQLITE_BUSY")
            raise refused
        await real(config, sent, task_id, task_path)

    monkeypatch.setattr(saver, "aput_writes", writes)


def failing_checkpoint_save(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, *, after_the_write: bool
) -> None:
    """The saver's first save of a checkpoint holding the decision refused, busy-coded: the
    one after the gate, or the first after the decision's write returned."""
    state = state_of(client)
    saver = state.checkpointer
    real_put = saver.aput
    real_record = state.drafts.record_decision
    written: list[bool] = []
    failed: list[bool] = []

    def record(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = real_record(*args, **kwargs)
        written.append(True)
        return result

    async def put(config: Any, checkpoint: Any, metadata: Any, versions: Any) -> Any:  # noqa: ANN401
        holds = checkpoint["channel_values"].get("decision") is not None
        if holds and not failed and bool(written) == after_the_write:
            failed.append(True)
            refused = coded("SQLITE_BUSY")
            raise refused
        return await real_put(config, checkpoint, metadata, versions)

    monkeypatch.setattr(state.drafts, "record_decision", record)
    monkeypatch.setattr(saver, "aput", put)


@pytest.mark.parametrize("failure", ["the resume's write", "the save after the write"])
def test_a_checkpoint_failure_after_the_decision_landed_is_said_to_be_recorded(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint file refuses a write that comes after, or doesn't stop, the decision's
    write: the decision is recorded, and the answer says so."""
    with a_waiting_plan() as (client, draft_id):
        if failure == "the resume's write":
            failing_checkpoint_writes(monkeypatch, client, channels=["__resume__"])
        else:
            failing_checkpoint_save(monkeypatch, client, after_the_write=True)
        answer = press(client, draft_id)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    the_stand_in(answer, status=503, said=APPROVED, note=None)
    assert after == ("approved", NOTE)


@pytest.mark.parametrize("failure", ["the save after the gate", "the run's opening read"])
def test_a_checkpoint_failure_before_the_decisions_write_cant_be_told(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint file refuses the save after the gate, or the write's own opening read:
    the row is still waiting, and from outside these look like a decision the thread holds, so
    the answer can't tell."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        if failure == "the save after the gate":
            failing_checkpoint_save(monkeypatch, client, after_the_write=False)
        else:
            saved = CountedAsync(
                state.checkpointer.aget_tuple, {2: raising_async(coded("SQLITE_BUSY"))}
            )
            monkeypatch.setattr(state.checkpointer, "aget_tuple", saved)
        answer = press(client, draft_id)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    the_stand_in(answer, status=500, said=UNKNOWN, note=NOTE)
    assert after == (None, None)


# ------------------------------------------------------------- the order of things


def test_the_reread_waits_for_every_write_and_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision's write raised and its failure is written to the checkpoint file slowly:
    the record is read again only once that write has ended, with the decision lock still
    held."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        saver = state.checkpointer
        real_writes = saver.aput_writes
        ended: list[float] = []
        reread: list[tuple[float, bool]] = []

        async def slow_failure(config: Any, sent: Any, task_id: str, task_path: str = "") -> None:  # noqa: ANN401
            if "__error__" in [str(channel) for channel, _ in sent]:
                await asyncio.sleep(0.4)
                await real_writes(config, sent, task_id, task_path)
                ended.append(time.monotonic())
                return
            await real_writes(config, sent, task_id, task_path)

        def read_again(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            reread.append((time.monotonic(), state.decision_lock.locked()))
            return real_get(*args, **kwargs)

        real_get = state.drafts.get
        monkeypatch.setattr(saver, "aput_writes", slow_failure)
        monkeypatch.setattr(state.drafts, "get", Counted(real_get, {2: read_again}))
        monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        answer = press(client, draft_id)

    assert answer.status_code == 500
    assert len(ended) == 1
    assert len(reread) == 1
    assert reread[0][0] > ended[0]
    assert reread[0][1] is True


HELD = 10.0
"""The most any step of the next test waits on another, so a run that never gets there fails
rather than hangs."""
A_WHILE = 0.5
"""How long the decision's worker stays held after the checkpoint's failure is raised, for a
run that would read again or let the lock go too early to do so first. A run that waits
passes however long this is; one that acts later than this after the failure isn't caught."""


def test_the_reread_and_the_lock_wait_for_a_worker_still_running_when_a_checkpoint_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume's checkpoint write fails while the decision's write is held on its worker
    thread: the record is read again, and the decision lock let go, only once that worker has
    finished, and the answer says the decision it recorded."""
    order: list[str] = []
    worker_started = threading.Event()
    failure_raised = threading.Event()
    worker_may_finish = threading.Event()
    too_early = threading.Event()
    held_at_reread: list[bool] = []

    def mark(step: str) -> None:
        order.append(step)
        if step in ("reread", "lock released") and "worker finished" not in order:
            too_early.set()

    def release_the_worker() -> None:
        if failure_raised.wait(HELD):
            too_early.wait(A_WHILE)
        worker_may_finish.set()

    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        saver = state.checkpointer
        real_writes = saver.aput_writes
        real_record = state.drafts.record_decision
        real_read_again = parent_routes.read_again
        lock = state.decision_lock
        real_release = lock.release

        def record(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            mark("worker started")
            worker_started.set()
            worker_may_finish.wait(HELD)
            result = real_record(*args, **kwargs)
            mark("worker finished")
            return result

        async def writes(config: Any, sent: Any, task_id: str, task_path: str = "") -> None:  # noqa: ANN401
            if [str(channel) for channel, _ in sent] == ["__resume__"]:
                await asyncio.to_thread(worker_started.wait, HELD)
                mark("checkpoint write failed")
                failure_raised.set()
                refused = coded("SQLITE_BUSY")
                raise refused
            await real_writes(config, sent, task_id, task_path)

        async def read_again(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            mark("reread")
            held_at_reread.append(lock.locked())
            await real_read_again(*args, **kwargs)

        def release() -> None:
            mark("lock released")
            real_release()

        monkeypatch.setattr(state.drafts, "record_decision", record)
        monkeypatch.setattr(saver, "aput_writes", writes)
        monkeypatch.setattr(parent_routes, "read_again", read_again)
        monkeypatch.setattr(lock, "release", release)
        releaser = threading.Thread(target=release_the_worker)
        releaser.start()
        try:
            answer = press(client, draft_id)
        finally:
            failure_raised.set()
            worker_may_finish.set()
            releaser.join(HELD)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    assert not releaser.is_alive()
    assert order == [
        "worker started",
        "checkpoint write failed",
        "worker finished",
        "reread",
        "lock released",
    ]
    assert held_at_reread == [True]
    assert not too_early.is_set()
    the_stand_in(answer, status=503, said=APPROVED, note=None)
    assert after == ("approved", NOTE)


JSON_CASES: dict[str, tuple[str, Callable[[], BaseException] | None]] = {
    "busy store": ("the write", lambda: StoreBusy("database is locked")),
    "busy writer": ("the write", lambda: WriterBusy("database is locked")),
    "busy commit": ("the write", lambda: coded("SQLITE_BUSY")),
    "unreadable write": ("the write", lambda: coded("SQLITE_IOERR")),
    "busy read after": ("the read after", lambda: StoreBusy("database is locked")),
    "slow read after": ("the read after", None),
}
"""Each failure the family page answers anew, asked through the JSON route instead."""


@pytest.mark.parametrize("case", list(JSON_CASES))
def test_the_json_route_answers_each_failure_as_it_did(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same failures asked through the JSON route: no second read, and each leaves the
    route as it always has, the store's failure raised, or the read that ran past the wait
    answered 503 with the record-too-slow words."""
    where, failure = JSON_CASES[case]
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        gets = Counted(state.drafts.get)
        if where == "the write":
            assert failure is not None
            error = failure()
            monkeypatch.setattr(state.drafts, "record_decision", raising(error))
        elif failure is not None:
            error = failure()
            gets.instead[2] = raising(error)
        else:
            monkeypatch.setattr(parent_routes, "STORE_WAIT_SECONDS", 0.2)
            gets.instead[2] = lambda *a, **k: time.sleep(1.0)
        monkeypatch.setattr(state.drafts, "get", gets)
        sent = {"approved": True, "reason": NOTE}
        if failure is None:
            answer = client.post(APPROVALS.format(draft_id), json=sent)
        else:
            with pytest.raises(type(error)) as caught:
                client.post(APPROVALS.format(draft_id), json=sent)
            assert caught.value is error
        monkeypatch.undo()
        time.sleep(1.0 if failure is None else 0.0)

    assert gets.calls == (1 if where == "the write" else 2)
    if failure is None:
        assert answer.status_code == 503
        assert answer.json() == {"detail": TOO_SLOW}


def test_a_failure_of_the_write_that_isnt_a_stores_is_the_servers_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that fails in a way no store fails is the server's error, with no second read."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        gets = Counted(state.drafts.get)
        monkeypatch.setattr(state.drafts, "get", gets)
        monkeypatch.setattr(state.drafts, "record_decision", raising(RuntimeError("a defect")))
        with pytest.raises(RuntimeError):
            press(client, draft_id)

    assert gets.calls == 1


def test_a_failure_of_the_first_read_that_isnt_a_stores_is_the_servers_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first read that fails in a way no store fails is the server's error."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        monkeypatch.setattr(
            state.drafts, "get", Counted(state.drafts.get, {1: raising(ValueError("a defect"))})
        )
        with pytest.raises(ValueError, match="a defect"):
            press(client, draft_id)


@pytest.mark.parametrize(
    ("case", "status", "said", "note"),
    [
        ("the row still waiting", 500, UNKNOWN, NOTE),
        ("the reread refused", 500, UNKNOWN, NOTE),
        ("this press's", 503, APPROVED, None),
        ("not this press's", 503, A_DECISION, NOTE),
        ("the read after a write that returned", 503, APPROVED, None),
    ],
)
def test_an_answer_after_the_write_reads_no_store_once_it_is_classified(
    case: str, status: int, said: str, note: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the record read again, or the read after a write that returned, has decided the
    answer, the answer is made from what the press already holds: the family page's builder
    and its fallbacks are never called, no statement reaches the four connections a page
    reads the household's file through, and the thread's saved state isn't read."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        real = state.drafts.get(draft_id)
        assert real is not None
        builders = {
            name: Counted(getattr(parent_routes, name))
            for name in ("review_page", "reviewed_once", "refused_on_the_page")
        }
        for name, counted in builders.items():
            monkeypatch.setattr(parent_routes, name, counted)
        saved_reads_then: list[int] = []
        real_get = state.drafts.get
        seconds: dict[str, Callable[..., Any]] = {
            "the row still waiting": real_get,
            "the reread refused": raising(StoreBusy()),
            "this press's": real_get,
            "not this press's": lambda *a, **k: recorded(real, "approved", "other words"),
            "the read after a write that returned": raising(StoreBusy()),
        }
        second = seconds[case]
        if case in ("the row still waiting", "the reread refused", "not this press's"):
            monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        elif case == "this press's":
            real_record = state.drafts.record_decision

            def landed_then_failed(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                real_record(*args, **kwargs)
                refused = coded("SQLITE_IOERR")
                raise refused

            monkeypatch.setattr(state.drafts, "record_decision", landed_then_failed)
        saved = CountedAsync(state.checkpointer.aget_tuple)
        monkeypatch.setattr(state.checkpointer, "aget_tuple", saved)
        with Statements(state) as seen:

            def deciding(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                try:
                    return second(*args, **kwargs)
                finally:
                    seen.append("FAILED HERE")
                    saved_reads_then.append(saved.calls)

            monkeypatch.setattr(state.drafts, "get", Counted(real_get, {2: deciding}))
            answer = press(client, draft_id)
        monkeypatch.undo()

    the_stand_in(answer, status=status, said=said, note=note)
    assert {name: counted.calls for name, counted in builders.items()} == {
        "review_page": 0,
        "reviewed_once": 0,
        "refused_on_the_page": 0,
    }
    assert after_the_failure(seen) == []
    assert saved.calls == saved_reads_then[0]


def test_a_disagreeing_press_after_a_refused_write_keeps_its_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Looks good, its write refused, leaves the decision in the thread; Ask for a change with
    other words then finishes that decision and is told it was already approved, the existing
    409 on the family page, with none of the new sentences."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        press(client, draft_id, "approve", NOTE)
        monkeypatch.undo()
        answer = press(client, draft_id, "refuse", "other words")
        after = row_of(client, draft_id)

    assert answer.status_code == 409, answer.text[:600]
    said = words(main_of(answer.text))
    assert f"draft '{draft_id}' was already approved" in said
    for new in (APPROVED, ASKED_FOR_A_CHANGE, A_DECISION, UNKNOWN):
        assert new not in said
    assert after == ("approved", NOTE)


def test_a_write_that_returns_with_nothing_recorded_keeps_its_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that returns and leaves the row waiting keeps the existing 500 that says the run
    resumed but no decision was recorded, with none of the new sentences."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        monkeypatch.setattr(state.drafts, "record_decision", lambda *a, **k: None)
        answer = press(client, draft_id)
        monkeypatch.undo()
        after = row_of(client, draft_id)

    assert answer.status_code == 500, answer.text[:600]
    said = words(main_of(answer.text))
    assert f"the run resumed but no decision was recorded for '{draft_id}'" in said
    for new in (APPROVED, ASKED_FOR_A_CHANGE, A_DECISION, UNKNOWN):
        assert new not in said
    assert after == (None, None)


# ------------------------------------------------------------ a real hold of the file


def test_a_write_whose_commit_waits_on_a_reader_cant_be_told(tmp_path: pathlib.Path) -> None:
    """Another connection reads the household's file through the press, so the decision's
    commit is refused as busy past the store's wait and rolled back: the row reads as waiting,
    and the answer can't tell."""
    with a_waiting_plan(**files_in(tmp_path)) as (client, draft_id):
        path = str(state_of(client).settings.database_path)
        reader = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM drafts").fetchall()
        try:
            answer = press(client, draft_id)
        finally:
            reader.execute("COMMIT")
            reader.close()
        after = row_of(client, draft_id)

    the_stand_in(answer, status=500, said=UNKNOWN, note=NOTE)
    assert after == (None, None)


def test_a_decision_pressed_while_the_drafts_store_is_held_says_the_record_was_slow() -> None:
    """The drafts store held by another caller through the press: whichever of the store's wait
    and the read's own wait ends first, the answer is that the record couldn't be read in time,
    503, and nothing is written."""
    with a_waiting_plan() as (client, draft_id):
        state = state_of(client)
        assert state.drafts._lock.acquire(timeout=5)
        try:
            answer = press(client, draft_id)
        finally:
            state.drafts._lock.release()
        after = row_of(client, draft_id)

    assert answer.status_code == 503, answer.text[:600]
    assert TOO_SLOW in words(main_of(answer.text))
    assert after == (None, None)


# ------------------------------------------------------------------ with the sign-in on


@pytest.mark.parametrize("outcome", ["can't be told", "recorded"])
def test_with_the_sign_in_on_each_new_answer_is_kept_from_caches(
    outcome: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent signed in: the answer that can't tell and the one that says the decision is
    recorded are each marked no-store, as the press's other answers are."""
    settings = replace(signed_in_household(tmp_path), anthropic_api_key="not-a-key-and-never-sent")
    app = create_app(settings)
    app.dependency_overrides[plan_graphs] = scripted_graphs(
        lambda: [fixture_week_plan()], lambda: [accepting()]
    )
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        signed_in(client, THEIRS)
        made = client.post("/parent/actions/plan", data=plan_form(client, "/parent"))
        assert made.status_code == 303, made.text[:300]
        record = state_of(client).drafts.latest_for(PLAN_DATE)
        assert record is not None
        state = state_of(client)
        if outcome == "can't be told":
            monkeypatch.setattr(state.drafts, "record_decision", raising(StoreBusy()))
        else:
            monkeypatch.setattr(
                state.drafts, "get", Counted(state.drafts.get, {2: raising(StoreBusy())})
            )
        answer = press(client, record.draft_id)

    assert answer.status_code == (500 if outcome == "can't be told" else 503), answer.text[:600]
    assert answer.headers["Cache-Control"] == "no-store"
