# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Saving homework from a screenshot review is one transaction for the homework and its
saved-outcome record: exactly the named statements, nothing of it after a refused write or a
crash at any point inside, serialized against other writers, replayed by its acceptance id, and
never undone or hidden by a failure after it commits.

Every review is read from synthetic screenshots of Wren's homework list.
"""

import dataclasses
import hashlib
import json
import pathlib
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

import pytest

from blossom import intake, screenshot_page, screenshot_review, screenshot_save
from blossom.grades.identity import name_form
from blossom.screenshot_review import Piece, carry, dates_key, piece_names, unit_name, value_json
from blossom.screenshot_save import (
    AlreadySaved,
    NotSaved,
    Replayed,
    ReviewState,
    Saved,
    SaveHooks,
    SavePress,
    confirm_subject,
    correction_digest,
    instruction_subject,
    piece_digest,
    review_state,
    save_homework,
    unit_digests,
)
from blossom.screenshots import homework_reading, homework_rows
from blossom.stores.import_drafts import (
    AnswerToKeep,
    ImportDraftsStore,
    NewImage,
    ReadTiming,
    ReviewChanged,
    UploadedReading,
    new_creation_token,
)
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import OBSERVED_AT, closed_world, fixture_clock, homework_answer, homework_card

HOMEWORK = "homework_screenshot"
TIMING = ReadTiming(seconds=1.0, input_tokens=10, output_tokens=10)
FRIDAY, MONDAY = date(2026, 8, 21), date(2026, 8, 24)
COURSE = "English 7"
WRITES = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})


class Ticks:
    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


@dataclasses.dataclass
class House:
    """One household file with her record and the draft store on it."""

    path: pathlib.Path
    record: ProjectStateStore
    drafts: ImportDraftsStore
    ticks: Ticks
    student: str

    def another_record(self) -> ProjectStateStore:
        """A second connection to the file, as another request's, that waits for no writer."""
        store = ProjectStateStore.open(self.path, fixture_clock())
        store._connection.execute("PRAGMA busy_timeout = 0")
        return store


@pytest.fixture
def house(tmp_path: pathlib.Path) -> Iterator[House]:
    path = tmp_path / "blossom.sqlite3"
    record = ProjectStateStore.open(path, fixture_clock())
    ticks = Ticks()
    drafts = ImportDraftsStore.open(
        path,
        fixture_clock(),
        ticks,
        rows={HOMEWORK: homework_rows},
        carries={HOMEWORK: carry},
        pieces={HOMEWORK: piece_names},
    )
    yield House(path, record, drafts, ticks, record.student_id())
    drafts.close()
    record.close()


def card(title: str, words: str, top: int) -> dict[str, object]:
    return homework_card(
        badge="DUE",
        course=COURSE,
        title=title,
        instruction=words,
        footer=None,
        y_top=top,
        y_bottom=top + 80,
    )


def screenshot(day: str, *cards: dict[str, object]) -> dict[str, object]:
    return homework_answer(headings=[{"text": day, "y": 150, "uncertain": None}], cards=list(cards))


def read_review(house: House, *answers: dict[str, object]) -> str:
    """A review of these screenshots, every one read."""
    labels = [hashlib.sha256(repr(a).encode()).hexdigest()[:12] for a in answers]
    images = [
        NewImage(png=f"synthetic-png-{label}".encode(), width=800, height=600) for label in labels
    ]
    added = house.drafts.create(
        house.student,
        kind=HOMEWORK,
        made_by="parent",
        creation_token=new_creation_token(),
        images=images,
    )
    assert added is not None
    house.ticks.at += 200.0
    snapshot = house.drafts.snapshot(house.student, added.draft_id)
    attempt = house.drafts.start_attempt(
        house.student, added.draft_id, [i.image_id for i in snapshot.images]
    )
    assert attempt is not None
    by_sha = {i.sha256: i.image_id for i in snapshot.images}
    for label, answer in zip(labels, answers, strict=True):
        checked = homework_reading(answer)
        assert checked.reading is not None
        sha = hashlib.sha256(f"synthetic-png-{label}".encode()).hexdigest()
        house.drafts.store_reading(
            house.student,
            added.draft_id,
            attempt_id=attempt.attempt_id,
            image_id=by_sha[sha],
            sha256=sha,
            reading=UploadedReading(
                checked.reading.image_kind,
                "v1b",
                "screenshot-reading/1",
                checked.reading.model_dump_json(),
            ),
            timing=TIMING,
        )
    house.drafts.end_attempt(house.student, added.draft_id, attempt.attempt_id)
    return added.draft_id


def state_of(house: House, draft_id: str, record: ProjectStateStore | None = None) -> ReviewState:
    store = record or house.record
    tables = store.draft_for_save(house.student, draft_id)
    assert tables is not None
    return review_state(store, tables, now=OBSERVED_AT, corrections={})


def keep_destinations(
    house: House, draft_id: str, chosen: Mapping[str, Mapping[str, object]]
) -> None:
    """Keep a press's posted destinations at the page's version, before any save."""
    version = house.drafts.snapshot(house.student, draft_id).version
    answers = [
        AnswerToKeep("destination", name, value_json(v), correction_digest_none(), (name,))
        for name, v in chosen.items()
    ]
    house.drafts.keep_answers(house.student, draft_id, version=version, answers=answers)


NAME_KEY: Final = b"synthetic-name-key-for-the-tests"
"""The key the tests' name forms are made under."""


def correction_digest_none() -> str:
    return hashlib.sha256(b"").hexdigest()[:12]


def press_of(
    house: House, draft_id: str, acceptance: str = "a" * 32, units: Sequence[str] | None = None
) -> SavePress:
    """The press a page made now would send, for every unit or the ones named."""
    state = state_of(house, draft_id)
    digests = unit_digests(state)
    return SavePress(
        house.student,
        draft_id,
        house.drafts.snapshot(house.student, draft_id).version,
        acceptance,
        tuple(units if units is not None else sorted(digests)),
        digests,
        {name: value_json(value) for name, value in state.defaults.items()},
        {},
        dict.fromkeys((c.sha256 for p in state.pieces for c in p.cards), ("matches", None)),
        NAME_KEY,
    )


def save(
    house: House,
    press: SavePress,
    hooks: SaveHooks | None = None,
    record: ProjectStateStore | None = None,
) -> object:
    extra = {} if hooks is None else {"hooks": hooks}
    return save_homework(
        record or house.record,
        house.drafts,
        press,
        clock=fixture_clock(),
        household=fixture_clock(),
        **extra,
    )


def world(house: House) -> dict[str, object]:
    return closed_world([house.path], leaving_out=())


def assignments(house: House) -> dict[str, Assignment]:
    return {a.assignment_id: a for a in house.record.all_assignments()}


def acceptances(house: House) -> list[str]:
    return [
        str(row[0])
        for row in sqlite3.connect(house.path).execute(
            "SELECT acceptance_id FROM grade_acceptances ORDER BY acceptance_id"
        )
    ]


def two_row_review(house: House, *, answer_due: bool = True) -> str:
    """A Vocab Quiz with no saved assignment, and a Reading Log that a saved Word Study row of
    another name, due Monday, is found for, with its due date moved to Friday."""
    house.record.upsert_assignments(
        [
            Assignment(
                assignment_id="assignment-word-study-x1",
                course=COURSE,
                title="Word Study",
                due_date=MONDAY,
                dependencies=[],
                reported_submission_status="not_reported",
                assigned_on=None,
                kind=AssignmentKind.HOMEWORK,
            )
        ]
    )
    draft_id = read_review(
        house,
        screenshot(
            "Friday 8/21/2026",
            card("Vocab Quiz", "Study list one.", 200),
            card("Reading Log", "Read pages 4 to 6.", 300),
        ),
    )
    state = state_of(house, draft_id)
    quiz = next(p for p in state.pieces if p.title == "Vocab Quiz")
    log = next(p for p in state.pieces if p.title == "Reading Log")
    keep_destinations(
        house,
        draft_id,
        {quiz.name: {"new": 1}, log.name: {"found": "assignment-word-study-x1", "offer": "basis"}},
    )
    if not answer_due:
        return draft_id
    state = state_of(house, draft_id)
    [question] = [q for u in state.validation.units for q in u.questions if q.kind == "due"]
    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=version,
        answers=[
            AnswerToKeep(
                "due",
                question.subject,
                FRIDAY.isoformat(),
                correction_digest_none(),
                question.pieces,
            )
        ],
    )
    return draft_id


# ------------------------------------------------------------------ what a save writes


def test_a_save_writes_the_homework_its_decision_its_pairs_and_deletes_the_answers(
    house: House,
) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id)

    outcome = save(house, press)

    assert isinstance(outcome, Saved)
    rows = assignments(house)
    found = rows["assignment-word-study-x1"]
    assert found.due_date == FRIDAY
    [new] = [a for a in rows.values() if a.title == "Vocab Quiz"]
    assert {target for _, target in outcome.pairs} == {
        new.assignment_id,
        "assignment-word-study-x1",
    }
    assert (
        house.record.homework_acceptance(house.student, press.draft_id, press.acceptance_id)
        == outcome.pairs
    )
    decided = house.record.intake_decisions([intake.pair(COURSE, "Reading Log")])
    assert [d.lands_on for answers in decided.values() for d in answers if d.kind == "renamed"] == [
        "assignment-word-study-x1"
    ]
    assert house.drafts.review_answers(house.student, draft_id).answers == () or outcome.closed
    assert outcome.closed


def test_the_save_counts_what_it_did_on_the_acceptance(house: House) -> None:
    draft_id = two_row_review(house)
    outcome = save(house, press_of(house, draft_id))
    assert isinstance(outcome, Saved)
    row = (
        sqlite3.connect(house.path)
        .execute(
            "SELECT identity_status, identity_answer, added, updated, already_saved, "
            "left_to_check, shown, "
            "answers_kept, complete, corrected FROM grade_acceptances"
        )
        .fetchone()
    )
    assert row == ("matches", "shown", 1, 1, 0, 0, 2, 3, 1, 0)


def test_a_pressed_group_with_a_question_still_open_is_refused_and_nothing_is_written(
    house: House,
) -> None:
    draft_id = two_row_review(house, answer_due=False)
    press = press_of(house, draft_id)
    state = state_of(house, draft_id)
    [waiting] = [unit_name(u.unit) for u in state.validation.units if u.questions]
    before = world(house)

    assert save(house, press) == NotSaved("open", (waiting,))
    assert world(house) == before


def test_yes_this_is_her_name_adds_the_keyed_form_as_the_homework_commits(house: House) -> None:
    draft_id = her_name_review(house)
    assert house.record.identity_of(NAME_KEY, "Wren").status.value == "first_use"

    assert isinstance(save(house, first_use(press_of(house, draft_id))), Saved)

    assert house.record.identity_of(NAME_KEY, "Wren").status.value == "matches"
    forms = sqlite3.connect(house.path).execute("SELECT * FROM grade_name_forms").fetchall()
    assert len(forms) == 1
    assert "Wren" not in repr(forms)
    assert identity_row(house) == ("first_use", "hers")


def identity_row(house: House) -> tuple[object, ...]:
    return tuple(
        sqlite3.connect(house.path)
        .execute("SELECT identity_status, identity_answer FROM grade_acceptances")
        .fetchone()
        or ()
    )


def named(press: SavePress, status: str, form: str | None) -> SavePress:
    """The press with every read screenshot's student line given this status and form."""
    return dataclasses.replace(press, names=dict.fromkeys(press.names, (status, form)))


def test_an_unconfirmed_name_saves_only_with_its_stored_answer_which_the_save_carries(
    house: House,
) -> None:
    draft_id = two_row_review(house)
    press = named(press_of(house, draft_id), "first_use", "f" * 64)
    before = world(house)

    assert save(house, press) == NotSaved("open", tuple(sorted(press.units)))
    assert world(house) == before

    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=version,
        answers=[AnswerToKeep("name", "f" * 64, "hers", correction_digest_none(), ())],
    )
    press = named(press_of(house, draft_id, "2" * 32), "first_use", "f" * 64)

    assert isinstance(save(house, press), Saved)
    assert identity_row(house) == ("first_use", "hers")


def test_screenshots_with_no_name_save_once_confirmed_and_a_line_never_checked_waits(
    house: House,
) -> None:
    draft_id = two_row_review(house)
    unchecked = dataclasses.replace(press_of(house, draft_id), names={})
    missing = named(press_of(house, draft_id), "missing", None)

    assert save(house, unchecked) == NotSaved("open", tuple(sorted(unchecked.units)))
    assert save(house, missing) == NotSaved("open", tuple(sorted(missing.units)))

    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=version,
        answers=[AnswerToKeep("name", "missing", "confirmed", correction_digest_none(), ())],
    )

    assert isinstance(
        save(house, named(press_of(house, draft_id, "3" * 32), "missing", None)), Saved
    )
    assert identity_row(house) == ("missing", "confirmed")


def test_units_needing_two_different_name_answers_are_refused_together(house: House) -> None:
    draft_id = read_review(
        house,
        screenshot("Friday 8/21/2026", card("Vocab Quiz", "Study list one.", 200)),
        screenshot("Friday 8/21/2026", card("Reading Log", "Read pages 4 to 6.", 200)),
    )
    state = state_of(house, draft_id)
    keep_destinations(house, draft_id, {p.name: {"new": 1} for p in state.pieces})
    press = press_of(house, draft_id)
    first, second = sorted(press.names)
    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=version,
        answers=[
            AnswerToKeep("name", "a" * 64, "hers", correction_digest_none(), ()),
            AnswerToKeep("name", "missing", "confirmed", correction_digest_none(), ()),
        ],
    )
    press = press_of(house, draft_id)
    both = {first: ("not_confirmed", "a" * 64), second: ("missing", None)}
    before = world(house)

    outcome = save(house, dataclasses.replace(press, names=both))

    assert outcome == NotSaved("names", tuple(sorted(press.units)))
    assert world(house) == before


def instruction_choice_review(house: House) -> str:
    """A Vocab Quiz saved with "Study list two.", then a second review whose card, with "Study
    list one.", goes to it: the record asks which instructions apply, and only the new one is
    chosen, so the earlier one is kept as history."""
    first = read_review(
        house, screenshot("Friday 8/21/2026", card("Vocab Quiz", "Study list two.", 200))
    )
    [piece] = state_of(house, first).pieces
    keep_destinations(house, first, {piece.name: {"new": 1}})
    saved = save(house, press_of(house, first, "7" * 32))
    assert isinstance(saved, Saved)
    [(_, target)] = saved.pairs
    second = read_review(
        house, screenshot("Friday 8/21/2026", card("Vocab Quiz", "Study list one.", 300))
    )
    [piece] = state_of(house, second).pieces
    keep_destinations(house, second, {piece.name: {"existing": target}})
    state = state_of(house, second)
    [(dest, change)] = [(d, c) for d, c in state.asked.items() if c is not None]
    assert change is not None
    subject = instruction_subject(dest, [piece.name], change.instructions_revision)
    chosen = json.dumps(["Study list one."], separators=(",", ":"))
    version = house.drafts.snapshot(house.student, second).version
    house.drafts.keep_answers(
        house.student,
        second,
        version=version,
        answers=[
            AnswerToKeep("instructions", subject, chosen, correction_digest_none(), (piece.name,))
        ],
    )
    return second


# ------------------------------------------------------------------ the transaction boundary

SITES: tuple[tuple[int, str], ...] = (
    (sqlite3.SQLITE_INSERT, "assignments"),
    (sqlite3.SQLITE_UPDATE, "assignments"),
    (sqlite3.SQLITE_INSERT, "date_claims"),
    (sqlite3.SQLITE_INSERT, "status_reports"),
    (sqlite3.SQLITE_INSERT, "school_instructions"),
    (sqlite3.SQLITE_INSERT, "intake_decisions"),
    (sqlite3.SQLITE_INSERT, "grade_acceptances"),
    (sqlite3.SQLITE_DELETE, "import_draft_answers"),
)
"""The write sites of a save of a new slot and a found row of another name: what the record
writes, the decision, the acceptance and the answer deletes."""
INSTRUCTION_SITES: tuple[tuple[int, str], ...] = (
    (sqlite3.SQLITE_UPDATE, "assignments"),
    (sqlite3.SQLITE_INSERT, "school_instructions"),
    (sqlite3.SQLITE_UPDATE, "school_instructions"),
    (sqlite3.SQLITE_INSERT, "grade_acceptances"),
    (sqlite3.SQLITE_DELETE, "import_draft_answers"),
)
"""The write sites of a save onto an existing assignment with an answered instruction question:
the new instruction, the earlier one kept as history, the acceptance and the answer deletes."""
WREN_FORM: Final = name_form(NAME_KEY, "Wren")
"""The keyed form of the student line the synthetic screenshots carry."""


def her_name_review(house: House) -> str:
    """The new slot and found row, from a screenshot whose name no form confirms yet, answered
    "Yes, this is her name"."""
    draft_id = two_row_review(house)
    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=version,
        answers=[AnswerToKeep("name", WREN_FORM, "hers", correction_digest_none(), ())],
    )
    return draft_id


def first_use(press: SavePress) -> SavePress:
    """The press with every screenshot's line read as a first use of her name."""
    return dataclasses.replace(press, names=dict.fromkeys(press.names, ("first_use", WREN_FORM)))


NAME_SITES: tuple[tuple[int, str], ...] = (
    *SITES,
    (sqlite3.SQLITE_INSERT, "grade_name_forms"),
    (sqlite3.SQLITE_UPDATE, "grade_student"),
)
"""The write sites of that save with "Yes, this is her name": its keyed form, and the key check
the first form sets."""
SHAPES: dict[
    str,
    tuple[Callable[[House], str], tuple[tuple[int, str], ...], Callable[[SavePress], SavePress]],
] = {
    "new slot and found row": (two_row_review, SITES, lambda press: press),
    "instruction choice": (instruction_choice_review, INSTRUCTION_SITES, lambda press: press),
    "her name confirmed": (her_name_review, NAME_SITES, first_use),
}
ALL_SITES = frozenset(
    {
        (sqlite3.SQLITE_INSERT, "assignments"),
        (sqlite3.SQLITE_UPDATE, "assignments"),
        (sqlite3.SQLITE_INSERT, "date_claims"),
        (sqlite3.SQLITE_INSERT, "status_reports"),
        (sqlite3.SQLITE_INSERT, "school_instructions"),
        (sqlite3.SQLITE_UPDATE, "school_instructions"),
        (sqlite3.SQLITE_INSERT, "intake_decisions"),
        (sqlite3.SQLITE_INSERT, "grade_acceptances"),
        (sqlite3.SQLITE_DELETE, "import_draft_answers"),
        (sqlite3.SQLITE_INSERT, "grade_name_forms"),
        (sqlite3.SQLITE_UPDATE, "grade_student"),
    }
)
"""The closed list: every write site the homework transaction makes, across the shapes."""
REFUSALS = [(shape, site) for shape, (_, sites, _) in SHAPES.items() for site in sites]


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_full_save_writes_exactly_the_named_statements(house: House, shape: str) -> None:
    build, expected, adjust = SHAPES[shape]
    draft_id = build(house)
    press = adjust(press_of(house, draft_id, "6" * 32))
    seen: set[tuple[int, str]] = set()

    def note(action: int, table: str | None, *_: object) -> int:
        if action in WRITES and table is not None:
            seen.add((action, table))
        return sqlite3.SQLITE_OK

    house.record._connection.set_authorizer(note)
    outcome = save(house, press)
    house.record._connection.set_authorizer(None)

    assert isinstance(outcome, Saved)
    assert seen == set(expected)


def test_the_closed_list_is_every_shapes_sites() -> None:
    assert {site for _, sites, _ in SHAPES.values() for site in sites} == ALL_SITES
    assert (sqlite3.SQLITE_UPDATE, "school_instructions") in ALL_SITES


@pytest.mark.parametrize(
    ("shape", "site"),
    REFUSALS,
    ids=[f"{shape}-{action}-{table}" for shape, (action, table) in REFUSALS],
)
def test_a_refused_write_at_any_statement_leaves_the_file_as_it_was(
    house: House, shape: str, site: tuple[int, str]
) -> None:
    """Refused at one write site, the save writes nothing, the acceptance pairs don't exist
    without the homework, and lifted, the same press saves once."""
    build, _, adjust = SHAPES[shape]
    draft_id = build(house)
    press = adjust(press_of(house, draft_id, "6" * 32))
    before = world(house)

    def refusing(action: int, table: str | None, *_: object) -> int:
        return sqlite3.SQLITE_DENY if (action, table) == site else sqlite3.SQLITE_OK

    house.record._connection.set_authorizer(refusing)
    outcome = save(house, press)
    house.record._connection.set_authorizer(None)

    assert outcome == NotSaved("failed")
    assert not house.record._connection.in_transaction
    assert world(house) == before
    assert isinstance(save(house, press), Saved)
    assert isinstance(save(house, press), Replayed)


# ------------------------------------------------------------------ a crash at each point


class Crash(BaseException):
    """A process stopping at a named point: no handler in the save catches it."""


class CrashAt(SaveHooks):
    def __init__(self, point: str, seen: Callable[[], None] = lambda: None) -> None:
        self.point = point
        self.seen = seen

    def _maybe(self, point: str) -> None:
        if point == self.point:
            self.seen()
            raise Crash(point)

    def after_keep(self, assignment_id: str) -> None:
        self._maybe("after_keep")

    def after_renamed(self, assignment_id: str) -> None:
        self._maybe("after_renamed")

    def before_acceptance(self) -> None:
        self._maybe("before_acceptance")

    def after_commit(self) -> None:
        self._maybe("after_commit")


@pytest.mark.parametrize("point", ["after_keep", "after_renamed", "before_acceptance"])
def test_a_crash_inside_the_transaction_leaves_the_file_as_it_was(house: House, point: str) -> None:
    """At the crash, another connection reads the file as it was, which is what a stopped
    process leaves; then the transaction is rolled back, and the same press saves once."""
    draft_id = two_row_review(house)
    press = press_of(house, draft_id)
    before = world(house)
    during: list[dict[str, object]] = []

    with pytest.raises(Crash):
        save(house, press, CrashAt(point, lambda: during.append(world(house))))

    assert during == [before]
    assert world(house) == before
    assert isinstance(save(house, press), Saved)


def test_a_process_exit_after_the_commit_keeps_the_save_and_a_resend_replays_it(
    house: House,
) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id)

    with pytest.raises(Crash):
        save(house, press, CrashAt("after_commit"))

    assert (
        house.record.homework_acceptance(house.student, press.draft_id, press.acceptance_id)
        is not None
    )
    snapshot = house.drafts.snapshot(house.student, draft_id)
    assert snapshot.finished
    assert snapshot.closed_at is None
    replay = save(house, press)
    assert isinstance(replay, Replayed)
    assert house.drafts.sweep().closed == 1


# ------------------------------------------------------------------ after the commit


def test_a_failed_close_keeps_and_reports_the_save_and_a_resend_replays_it(house: House) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id)

    def busy(*args: object, **kwargs: object) -> bool:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    house.drafts.clean_up = busy  # type: ignore[method-assign]
    outcome = save(house, press)

    assert isinstance(outcome, Saved)
    assert (outcome.closed, outcome.incomplete) == (False, ("close",))
    assert (
        house.record.homework_acceptance(house.student, press.draft_id, press.acceptance_id)
        == outcome.pairs
    )
    assert house.drafts.snapshot(house.student, draft_id).finished
    assert isinstance(save(house, press), Replayed)
    assert house.drafts.sweep().closed == 1


def test_a_later_write_failing_after_the_commit_never_hides_the_save(house: House) -> None:
    class LaterWriteFails(SaveHooks):
        def after_commit(self) -> None:
            msg = "a later write failed"
            raise RuntimeError(msg)

    draft_id = two_row_review(house)
    outcome = save(house, press_of(house, draft_id), LaterWriteFails())

    assert isinstance(outcome, Saved)
    assert outcome.incomplete == ("close",)
    assert len(acceptances(house)) == 1


# ------------------------------------------------------------------ retries


def test_a_resent_press_replays_by_its_acceptance_id(house: House) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id)
    first = save(house, press)
    assert isinstance(first, Saved)
    before = world(house)

    again = save(house, press)

    assert again == Replayed(press.acceptance_id, first.pairs)
    assert world(house) == before


def test_a_fresh_press_after_a_lost_response_writes_nothing_twice(house: House) -> None:
    draft_id = read_review(
        house,
        screenshot(
            "Friday 8/21/2026",
            card("Vocab Quiz", "Study list one.", 200),
            card("Reading Log", "Read pages 4 to 6.", 300),
        ),
    )
    state = state_of(house, draft_id)
    keep_destinations(house, draft_id, {p.name: {"new": 1} for p in state.pieces})
    quiz = next(p.name for p in state.pieces if p.title == "Vocab Quiz")
    first = press_of(house, draft_id, "b" * 32, units=[quiz])
    assert isinstance(save(house, first), Saved)
    before = world(house)

    fresh = dataclasses.replace(first, acceptance_id="c" * 32)

    assert save(house, fresh) == AlreadySaved((quiz,))
    assert world(house) == before


def test_an_old_post_is_refused_by_step_twos_version_check(house: House) -> None:
    draft_id = read_review(
        house, screenshot("Friday 8/21/2026", card("Vocab Quiz", "Study list one.", 200))
    )
    [piece] = state_of(house, draft_id).pieces
    version = house.drafts.snapshot(house.student, draft_id).version
    answer = AnswerToKeep(
        "destination", piece.name, value_json({"new": 1}), correction_digest_none(), (piece.name,)
    )
    house.drafts.keep_answers(house.student, draft_id, version=version, answers=[answer])

    with pytest.raises(ReviewChanged):
        house.drafts.keep_answers(house.student, draft_id, version=version, answers=[answer])


def test_a_stale_page_a_closed_gate_and_a_changed_effect_save_nothing(house: House) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id)
    before = world(house)

    assert save(house, dataclasses.replace(press, version=press.version - 1)) == NotSaved("stale")
    changed = save(
        house, dataclasses.replace(press, digests=dict.fromkeys(press.digests, ("0" * 64,)))
    )
    assert isinstance(changed, NotSaved)
    assert changed.reason == "changed"
    assert world(house) == before

    house.drafts.add_images(
        house.student, draft_id, [NewImage(png=b"synthetic-png-unread", width=800, height=600)]
    )
    unread = press_of(house, draft_id, "8" * 32)
    gated = world(house)
    assert save(house, unread) == NotSaved("gate")
    assert world(house) == gated


STAYS: Final = "Updates Word Study. Stays homework, as saved."


def heads_of(shown: screenshot_page.RowsShown) -> list[tuple[str, str, tuple[str, ...]]]:
    """Each chosen destination's class, title and quiet lines, as her week's card shows them."""
    return [
        (block.head.course, block.head.title, block.head.quiet)
        for unit in shown.units
        for block in unit.blocks
        if block.chosen
    ]


@pytest.mark.parametrize(
    ("change", "update"),
    [
        ("kind", {"kind": AssignmentKind.TASK}),
        ("title", {"title": "Word Study Guide"}),
        ("class", {"course": "English 7 Honors"}),
    ],
    ids=["kind", "title", "class"],
)
def test_a_saved_assignment_changed_elsewhere_after_the_preview_saves_nothing(
    house: House, change: str, update: Mapping[str, object]
) -> None:
    """The Reading Log card goes to the saved Word Study row found for it, its kind left to the
    saved row's. Another tab changes that row's kind, title or class after the page showed its
    lines: the press is refused as changed and writes nothing, the page's lines say the change,
    and a press from that page saves."""
    draft_id = two_row_review(house)
    log = next(p for p in state_of(house, draft_id).pieces if p.title == "Reading Log")
    units = [u for u in unit_digests(state_of(house, draft_id)) if log.name in u.split("+")]
    press = press_of(house, draft_id, units=units)
    lines = heads_of(page_of(house, draft_id))
    [saved] = [
        a for a in house.record.all_assignments() if a.assignment_id == "assignment-word-study-x1"
    ]
    house.record.upsert_assignments([saved.model_copy(update=dict(update))])
    before = world(house)
    outcome = save(house, press)
    after = world(house)
    shown = page_of(house, draft_id)
    again = save(house, press_of(house, draft_id, "9" * 32, units=units))

    assert ("English 7", "Word Study", ("No assigned date.", STAYS)) in lines
    assert isinstance(outcome, NotSaved)
    assert outcome.reason == "changed"
    assert after == before
    assert heads_of(shown) != lines
    assert isinstance(again, Saved)


def test_a_press_with_a_unit_saved_elsewhere_and_a_changed_one_is_not_already_saved(
    house: House,
) -> None:
    """One pressed unit was saved by another press and the other's effect changed: the press is
    refused as changed, naming both, and writes nothing."""
    draft_id = two_row_review(house)
    state = state_of(house, draft_id)
    quiz, log = (
        next(
            unit_name(u.unit)
            for u in state.validation.units
            if any(state.by_name[n].title == title for n in u.unit)
        )
        for title in ("Vocab Quiz", "Reading Log")
    )
    first = save(house, press_of(house, draft_id, "4" * 32, units=[quiz]))
    assert isinstance(first, Saved)
    assert first.saved_units == (quiz,)
    press = press_of(house, draft_id, "5" * 32, units=[log])
    mixed = dataclasses.replace(press, units=(quiz, log), digests={log: ("0" * 64,)})
    before = world(house)

    outcome = save(house, mixed)

    assert outcome == NotSaved("changed", (log,), (quiz,))
    assert world(house) == before


# ------------------------------------------------------------------ concurrent writers


def held_save(
    house: House, press: SavePress
) -> tuple[threading.Thread, threading.Event, threading.Event, list[object]]:
    """A save that stops inside its transaction, holding the writer, until released."""
    holding, release, result = threading.Event(), threading.Event(), []

    class Hold(SaveHooks):
        def before_acceptance(self) -> None:
            holding.set()
            assert release.wait(timeout=30)

    thread = threading.Thread(target=lambda: result.append(save(house, press, Hold())))
    thread.start()
    assert holding.wait(timeout=30)
    return thread, holding, release, result


def test_two_presses_of_one_review_serialize_and_the_second_writes_nothing_twice(
    house: House,
) -> None:
    draft_id = two_row_review(house)
    log = next(p.name for p in state_of(house, draft_id).pieces if p.title == "Reading Log")
    first = press_of(house, draft_id, "d" * 32, units=[log])
    second = dataclasses.replace(first, acceptance_id="e" * 32)
    other = house.another_record()
    try:
        thread, _, release, result = held_save(house, first)
        assert save(house, second, record=other) == NotSaved("busy")
        release.set()
        thread.join(timeout=30)
        assert isinstance(result[0], Saved)
        assert save(house, second, record=other) == AlreadySaved(second.units)
    finally:
        other.close()
    assert len(acceptances(house)) == 1


def test_a_press_racing_another_reviews_save_sees_its_commit(house: House) -> None:
    house.record.upsert_assignments(
        [
            Assignment(
                assignment_id="assignment-vocab-quiz-r1",
                course=COURSE,
                title="Vocab Quiz",
                due_date=FRIDAY,
                dependencies=[],
                reported_submission_status="not_reported",
                assigned_on=None,
                kind=AssignmentKind.HOMEWORK,
            )
        ]
    )
    one = read_review(
        house, screenshot("Friday 8/21/2026", card("Vocab Quiz", "Study list one.", 200))
    )
    two = read_review(
        house, screenshot("Friday 8/21/2026", card("Vocab Quiz", "Study list two.", 300))
    )
    for draft_id in (one, two):
        [piece] = state_of(house, draft_id).pieces
        keep_destinations(house, draft_id, {piece.name: {"existing": "assignment-vocab-quiz-r1"}})
    first, second = press_of(house, one, "f" * 32), press_of(house, two, "0" * 32)
    other = house.another_record()
    try:
        thread, _, release, result = held_save(house, first)
        assert save(house, second, record=other) == NotSaved("busy")
        release.set()
        thread.join(timeout=30)
        assert isinstance(result[0], Saved)
        late = save(house, second, record=other)
        assert isinstance(late, NotSaved)
        assert late.reason == "changed"
    finally:
        other.close()
    assert len(acceptances(house)) == 1


def test_a_press_and_an_outside_change_serialize_in_either_order(house: House) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id)
    other = house.another_record()
    try:
        thread, _, release, result = held_save(house, press)
        moved = Assignment(
            assignment_id="assignment-word-study-x1",
            course=COURSE,
            title="Word Study",
            due_date=date(2026, 8, 26),
            dependencies=[],
            reported_submission_status="not_reported",
            assigned_on=None,
            kind=AssignmentKind.HOMEWORK,
        )
        with pytest.raises(sqlite3.OperationalError):
            other.upsert_assignments([moved])
        release.set()
        thread.join(timeout=30)
        assert isinstance(result[0], Saved)
        assert assignments(house)["assignment-word-study-x1"].due_date == FRIDAY
    finally:
        other.close()

    later = two_row_review_again(house)
    page = press_of(house, later, "9" * 32)
    house.record.upsert_assignments(
        [
            assignments(house)["assignment-word-study-x1"].model_copy(
                update={"due_date": date(2026, 8, 26)}
            )
        ]
    )
    outcome = save(house, page)
    assert isinstance(outcome, NotSaved)
    assert outcome.reason == "changed"


def two_row_review_again(house: House) -> str:
    """Another review of a Reading Log found for the saved Word Study row, due Friday."""
    draft_id = read_review(
        house, screenshot("Friday 8/21/2026", card("Reading Log", "Read pages 7 to 9.", 400))
    )
    [piece] = state_of(house, draft_id).pieces
    keep_destinations(
        house, draft_id, {piece.name: {"found": "assignment-word-study-x1", "offer": "basis"}}
    )
    return draft_id


# ------------------------------------------------------------------ what the acceptance records


def unsure(
    title: str,
    words: str,
    top: int,
    *fields: str,
    badge: str = "ASSIGNED",
    footer: str = "Due (8/24/2026)",
) -> dict[str, object]:
    """A card of English 7 under Friday's heading, due Monday, whose named fields the reader
    wasn't sure of."""
    return homework_card(
        badge=badge,
        course=COURSE,
        title=title,
        instruction=words,
        footer=footer,
        y_top=top,
        y_bottom=top + 80,
        uncertain=[{"field": field, "reason": "blurred"} for field in fields],
    )


def keys_of(house: House, draft_id: str) -> dict[str, str]:
    """Each piece's row key, by its title as read."""
    return {p.title: p.key for p in state_of(house, draft_id).pieces}


def answered_press(
    house: House,
    draft_id: str,
    corrections: Mapping[str, Mapping[str, str | None]],
    confirmed: Sequence[tuple[str, str]] = (),
    *,
    acceptance: str = "c" * 32,
    rows: Sequence[str] | None = None,
    destinations: Mapping[str, Mapping[str, object]] | None = None,
    due_read: Mapping[str, str] | None = None,
) -> SavePress:
    """Every piece kept on its destination (its own new slot unless named) and the named fields
    confirmed as read, each answer bound to its row's corrections; then the press a page made
    with those corrections sends, for the groups of the row keys named, or all."""
    tables = house.record.draft_for_save(house.student, draft_id)
    assert tables is not None
    state = review_state(house.record, tables, now=OBSERVED_AT, corrections=corrections)
    by_key = {p.key: p for p in state.pieces}
    answers = [
        AnswerToKeep(
            "destination",
            piece.name,
            value_json(dict((destinations or {}).get(piece.title, {"new": 1}))),
            piece_digest(piece, corrections),
            (piece.name,),
        )
        for piece in state.open
    ]
    answers += [
        AnswerToKeep(
            "confirm",
            confirm_subject(key, field),
            "as_read",
            correction_digest(corrections.get(key)),
            (by_key[key].name,),
        )
        for key, field in confirmed
    ]
    answers += [
        AnswerToKeep(
            "due_read",
            piece.name,
            (due_read or {})[piece.key],
            correction_digest(corrections.get(piece.key)),
            (piece.name,),
        )
        for piece in state.open
        if piece.key in (due_read or {})
    ]
    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(house.student, draft_id, version=version, answers=answers)
    tables = house.record.draft_for_save(house.student, draft_id)
    assert tables is not None
    state = review_state(house.record, tables, now=OBSERVED_AT, corrections=corrections)
    digests = unit_digests(state)
    chosen = sorted(
        unit_name(u.unit)
        for u in state.validation.units
        if rows is None or any(state.by_name[n].key in rows for n in u.unit)
    )
    return SavePress(
        house.student,
        draft_id,
        house.drafts.snapshot(house.student, draft_id).version,
        acceptance,
        tuple(chosen),
        digests,
        {name: value_json(value) for name, value in state.defaults.items()},
        {key: dict(fields) for key, fields in corrections.items()},
        dict.fromkeys((c.sha256 for p in state.pieces for c in p.cards), ("matches", None)),
        NAME_KEY,
    )


def counts_recorded(house: House, acceptance: str) -> tuple[object, ...]:
    return tuple(
        sqlite3.connect(house.path)
        .execute(
            "SELECT corrected, confirmed FROM grade_acceptances WHERE acceptance_id = ?",
            (acceptance,),
        )
        .fetchone()
        or ()
    )


def corrected_and_confirmed_press(house: House) -> SavePress:
    """Two fields corrected and three confirmed as read, on the two rows the press saves."""
    draft_id = read_review(
        house,
        screenshot(
            "Friday 8/21/2026",
            unsure(
                "Vocab Quiz", "Study list one.", 200, "title", "footer", "instruction", "course"
            ),
            unsure("Reading Log", "Read pages 4 to 6.", 300, "title"),
        ),
    )
    keys = keys_of(house, draft_id)
    quiz, log = keys["Vocab Quiz"], keys["Reading Log"]
    corrections = {quiz: {"title": "Vocab Test", "footer": "2026-08-25"}}
    confirmed = [(quiz, "instruction"), (quiz, "course"), (log, "title")]
    return answered_press(house, draft_id, corrections, confirmed)


def fields_corrected_and_confirmed(house: House) -> tuple[tuple[object, ...], tuple[int, int]]:
    press = corrected_and_confirmed_press(house)
    assert isinstance(save(house, press), Saved)
    return counts_recorded(house, press.acceptance_id), (2, 3)


def read_values_typed_back(house: House) -> tuple[tuple[object, ...], tuple[int, int]]:
    draft_id = read_review(
        house,
        screenshot(
            "Friday 8/21/2026",
            unsure("Vocab Quiz", "Study list one.", 200, "title", "footer", "instruction"),
        ),
    )
    quiz = keys_of(house, draft_id)["Vocab Quiz"]
    typed = {"title": "Vocab Quiz", "footer": "2026-08-24", "instruction": "Study list one."}
    press = answered_press(house, draft_id, {quiz: typed})
    assert isinstance(save(house, press), Saved)
    return counts_recorded(house, press.acceptance_id), (0, 0)


def unreadable_date_confirmed(house: House) -> tuple[tuple[object, ...], tuple[int, int]]:
    draft_id = read_review(
        house,
        screenshot("Friday 8/21/2026", unsure("Vocab Quiz", "Study list one.", 200, "footer")),
    )
    quiz = keys_of(house, draft_id)["Vocab Quiz"]
    press = answered_press(house, draft_id, {quiz: {"footer": "10/14"}}, [(quiz, "footer")])
    assert isinstance(save(house, press), Saved)
    assert next(a for a in house.record.all_assignments()).due_date == MONDAY
    return counts_recorded(house, press.acceptance_id), (0, 1)


def due_date_chosen_among_those_read(house: House) -> tuple[tuple[object, ...], tuple[int, int]]:
    draft_id = read_review(
        house,
        screenshot(
            "Friday 8/21/2026", unsure("Vocab Quiz", "Study list one.", 200, "footer", badge="DUE")
        ),
    )
    quiz = keys_of(house, draft_id)["Vocab Quiz"]
    press = answered_press(house, draft_id, {quiz: {"footer": FRIDAY.isoformat()}})
    assert isinstance(save(house, press), Saved)
    assert next(a for a in house.record.all_assignments()).due_date == FRIDAY
    return counts_recorded(house, press.acceptance_id), (0, 0)


def assigned_date_corrected_to_the_due_card_s_heading(
    house: House,
) -> tuple[tuple[object, ...], tuple[int, int]]:
    """One item read as an assigned card under a Friday heading the reader wasn't sure of, due
    Monday, and as the due card under Monday whose own due line says Tuesday: the assigned date
    corrected to Monday is a correction, though Monday is the due card's heading."""
    draft_id = read_review(
        house,
        homework_answer(
            headings=[
                {"text": "Friday 8/21/2026", "y": 150, "uncertain": "faint"},
                {"text": "Monday 8/24/2026", "y": 400, "uncertain": None},
            ],
            cards=[
                unsure("Vocab Quiz", "Study list one.", 200),
                unsure("Vocab Quiz", "Study list one.", 450, badge="DUE", footer="Due (8/25/2026)"),
            ],
        ),
    )
    quiz = keys_of(house, draft_id)["Vocab Quiz"]
    press = answered_press(
        house,
        draft_id,
        {quiz: {"heading": MONDAY.isoformat()}},
        due_read={quiz: date(2026, 8, 25).isoformat()},
    )
    assert isinstance(save(house, press), Saved)
    [written] = house.record.all_assignments()
    assert (written.due_date, written.assigned_on) == (date(2026, 8, 25), MONDAY)
    return counts_recorded(house, press.acceptance_id), (1, 0)


def rows_this_save_did_not_write(house: House) -> tuple[tuple[object, ...], tuple[int, ...]]:
    """An unselected row, then a row another save accepted, then a destination that writes
    nothing: only the row each save writes counts."""
    draft_id = read_review(
        house,
        screenshot(
            "Friday 8/21/2026",
            unsure("Vocab Quiz", "Study list one.", 200, "title"),
            unsure("Reading Log", "Read pages 4 to 6.", 300, "title"),
        ),
    )
    keys = keys_of(house, draft_id)
    corrections = {
        keys["Vocab Quiz"]: {"title": "Vocab Test"},
        keys["Reading Log"]: {"title": "Reading Notes"},
    }
    first = answered_press(house, draft_id, corrections, rows=[keys["Vocab Quiz"]])
    assert isinstance(save(house, first), Saved)
    second = answered_press(house, draft_id, corrections, acceptance="d" * 32)
    assert isinstance(save(house, second), Saved)
    [test] = [a for a in house.record.all_assignments() if a.title == "Vocab Test"]
    again = read_review(
        house,
        screenshot("Friday 8/21/2026", unsure("Vocab Test", "Study list one.", 200, "course")),
    )
    on = {"Vocab Test": {"existing": test.assignment_id}}
    course = keys_of(house, again)["Vocab Test"]
    third = answered_press(
        house, again, {}, [(course, "course")], acceptance="e" * 32, destinations=on
    )
    assert isinstance(save(house, third), Saved)
    got = (
        *counts_recorded(house, first.acceptance_id),
        *counts_recorded(house, second.acceptance_id),
        *counts_recorded(house, third.acceptance_id),
    )
    return got, (1, 0, 1, 0, 0, 0)


def replayed_unchanged(house: House) -> tuple[tuple[object, ...], tuple[int, ...]]:
    press = corrected_and_confirmed_press(house)
    assert isinstance(save(house, press), Saved)
    assert counts_recorded(house, press.acceptance_id) == (2, 3)
    assert isinstance(save(house, press), Replayed)
    return counts_recorded(house, press.acceptance_id), (2, 3)


def fold_corrections_on_a_confident_row(
    house: House,
) -> tuple[tuple[object, ...], tuple[int, int]]:
    """A row read with confidence, its title and its own due date corrected in the fold, and
    its instruction text typed back as read: two boxes corrected, nothing confirmed."""
    draft_id = read_review(
        house, screenshot("Friday 8/21/2026", unsure("Vocab Quiz", "Study list one.", 200))
    )
    [piece] = state_of(house, draft_id).pieces
    corrections = {
        piece.key: {"title": "Vocab Test", "instruction:0": "Study list one."},
        dates_key(piece.name): {"due": "2026-08-25"},
    }
    press = answered_press(house, draft_id, corrections)
    assert isinstance(save(house, press), Saved)
    [written] = house.record.all_assignments()
    assert (written.title, written.due_date) == ("Vocab Test", date(2026, 8, 25))
    return counts_recorded(house, press.acceptance_id), (2, 0)


def a_removal_answers_an_unsure_instruction(
    house: House,
) -> tuple[tuple[object, ...], tuple[int, int]]:
    """An instruction the reader wasn't sure of, removed in the fold: a correction, which
    answers the field, so nothing is confirmed."""
    draft_id = read_review(
        house,
        screenshot("Friday 8/21/2026", unsure("Vocab Quiz", "Study list one.", 200, "instruction")),
    )
    [piece] = state_of(house, draft_id).pieces
    press = answered_press(house, draft_id, {piece.key: {"instruction:0": None}})
    assert isinstance(save(house, press), Saved)
    return counts_recorded(house, press.acceptance_id), (1, 0)


COUNT_CASES: Final[dict[str, Callable[[House], tuple[tuple[object, ...], tuple[int, ...]]]]] = {
    "fold corrections on a confident row": fold_corrections_on_a_confident_row,
    "a removal answers an unsure instruction": a_removal_answers_an_unsure_instruction,
    "two corrected and three confirmed": fields_corrected_and_confirmed,
    "the read values typed back": read_values_typed_back,
    "an unreadable date confirmed": unreadable_date_confirmed,
    "a due date chosen among those read": due_date_chosen_among_those_read,
    "an assigned date corrected to the due card's heading": (
        assigned_date_corrected_to_the_due_card_s_heading
    ),
    "rows this save didn't write": rows_this_save_did_not_write,
    "a replay": replayed_unchanged,
}


@pytest.mark.parametrize("case", sorted(COUNT_CASES))
def test_the_acceptance_counts_corrected_and_confirmed_fields(house: House, case: str) -> None:
    got, expected = COUNT_CASES[case](house)

    assert got == expected


def test_a_correction_blossom_can_t_use_answers_no_field(house: House) -> None:
    draft_id = read_review(
        house,
        screenshot("Friday 8/21/2026", unsure("Vocab Quiz", "Study list one.", 200, "footer")),
    )
    quiz = keys_of(house, draft_id)["Vocab Quiz"]
    press = answered_press(house, draft_id, {quiz: {"footer": "10/14"}})
    before = world(house)

    assert save(house, press) == NotSaved("open", press.units)
    assert world(house) == before


OTHER_KEY: Final = b"synthetic-name-key-made-again-two"
"""A second household key, as after the secret is replaced."""


def form_recorded(house: House, acceptance: str) -> tuple[object, ...]:
    return tuple(
        sqlite3.connect(house.path)
        .execute(
            "SELECT identity_status, identity_answer, identity_form FROM grade_acceptances "
            "WHERE acceptance_id = ?",
            (acceptance,),
        )
        .fetchone()
        or ()
    )


def forms_kept(house: House) -> list[str]:
    return [
        str(row[0])
        for row in sqlite3.connect(house.path).execute("SELECT name_form FROM grade_name_forms")
    ]


def name_answered(house: House, draft_id: str, form: str, answer: str) -> None:
    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=version,
        answers=[AnswerToKeep("name", form, answer, correction_digest_none(), ())],
    )


def first_use_answered_hers(house: House) -> tuple[object, ...]:
    press = first_use(press_of(house, her_name_review(house), "1" * 32))
    assert isinstance(save(house, press), Saved)
    assert forms_kept(house) == [WREN_FORM]
    return form_recorded(house, press.acceptance_id), ("first_use", "hers", WREN_FORM)


def misread(house: House) -> tuple[object, ...]:
    draft_id = two_row_review(house)
    name_answered(house, draft_id, WREN_FORM, "misread")
    press = first_use(press_of(house, draft_id, "1" * 32))
    assert isinstance(save(house, press), Saved)
    assert forms_kept(house) == []
    return form_recorded(house, press.acceptance_id), ("first_use", "misread", WREN_FORM)


def confirmed_again(house: House) -> tuple[object, ...]:
    first_use_answered_hers(house)
    again = name_form(OTHER_KEY, "Wren")
    draft_id = read_review(
        house, screenshot("Friday 8/21/2026", card("Word Study", "Study list three.", 200))
    )
    keep_destinations(
        house, draft_id, {p.name: {"new": 1} for p in state_of(house, draft_id).pieces}
    )
    name_answered(house, draft_id, again, "hers")
    press = dataclasses.replace(
        press_of(house, draft_id, "2" * 32),
        name_key=OTHER_KEY,
    )
    press = dataclasses.replace(press, names=dict.fromkeys(press.names, ("confirm_again", again)))
    assert isinstance(save(house, press), Saved)
    return form_recorded(house, press.acceptance_id), ("confirm_again", "hers", again)


def matches(house: House) -> tuple[object, ...]:
    first_use_answered_hers(house)
    draft_id = read_review(
        house, screenshot("Friday 8/21/2026", card("Word Study", "Study list three.", 200))
    )
    keep_destinations(
        house, draft_id, {p.name: {"new": 1} for p in state_of(house, draft_id).pieces}
    )
    press = named(press_of(house, draft_id, "2" * 32), "matches", WREN_FORM)
    assert isinstance(save(house, press), Saved)
    assert forms_kept(house) == [WREN_FORM]
    return form_recorded(house, press.acceptance_id), ("matches", "shown", WREN_FORM)


def missing(house: House) -> tuple[object, ...]:
    draft_id = two_row_review(house)
    name_answered(house, draft_id, "missing", "confirmed")
    press = named(press_of(house, draft_id, "1" * 32), "missing", None)
    assert isinstance(save(house, press), Saved)
    return form_recorded(house, press.acceptance_id), ("missing", "confirmed", None)


def replayed_form(house: House) -> tuple[object, ...]:
    press = first_use(press_of(house, her_name_review(house), "1" * 32))
    assert isinstance(save(house, press), Saved)
    recorded = form_recorded(house, press.acceptance_id)
    assert isinstance(save(house, press), Replayed)
    return form_recorded(house, press.acceptance_id), recorded


FORM_CASES: Final = {
    "first use answered hers": first_use_answered_hers,
    "misread": misread,
    "confirm again": confirmed_again,
    "matches": matches,
    "missing": missing,
    "a replay": replayed_form,
}


@pytest.mark.parametrize("case", sorted(FORM_CASES))
def test_the_acceptance_records_the_keyed_form_it_answered_or_matched(
    house: House, case: str
) -> None:
    got, expected = FORM_CASES[case](house)
    row = sqlite3.connect(house.path).execute("SELECT * FROM grade_acceptances").fetchall()

    assert got == expected
    assert "Wren" not in repr(row)


def test_check_zero_compares_student_and_source_key(house: House) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id, "4" * 32)
    saved = save(house, press)
    assert isinstance(saved, Saved)
    other = read_review(
        house, screenshot("Friday 8/21/2026", card("Word Study", "Study list three.", 200))
    )
    keep_destinations(house, other, {p.name: {"new": 1} for p in state_of(house, other).pieces})
    stranger = dataclasses.replace(press_of(house, other, "4" * 32))
    before = world(house)

    outcome = save(house, stranger)

    assert house.record.homework_acceptance(house.student, draft_id, "4" * 32) == saved.pairs
    assert house.record.homework_acceptance(house.student, other, "4" * 32) is None
    assert house.record.homework_acceptance("another-student", draft_id, "4" * 32) is None
    assert house.record.homework_counts(house.student, draft_id, "4" * 32) is not None
    assert house.record.homework_counts(house.student, other, "4" * 32) is None
    assert house.record.homework_counts("another-student", draft_id, "4" * 32) is None
    assert not isinstance(outcome, Replayed)
    assert world(house) == before


# ----------------------------------------------- one row from a named and an unnamed screenshot

AVERY_FORM: Final = name_form(NAME_KEY, "Avery")
"""The keyed form of a second, different student line."""


def sha_of(answer: dict[str, object]) -> str:
    """The SHA-256 ``read_review`` stores a screenshot of this answer under."""
    label = hashlib.sha256(repr(answer).encode()).hexdigest()[:12]
    return hashlib.sha256(f"synthetic-png-{label}".encode()).hexdigest()


def shown_by(name: str | None, heading: str, *cards: dict[str, object]) -> dict[str, object]:
    """A screenshot naming this student, or none, under one day heading."""
    return homework_answer(
        active_student={"name": name, "uncertain": None},
        headings=[{"text": heading, "y": 100, "uncertain": None}],
        cards=list(cards),
    )


ASSIGNED_TUESDAY: Final = homework_card(footer="Due (10/7/2026)", y_top=150, y_bottom=230)
DUE_WEDNESDAY: Final = homework_card(
    badge="DUE", instruction=None, footer=None, y_top=150, y_bottom=230
)


def merged_review(house: House, second: str | None = None) -> tuple[str, str, str]:
    """One Sentence Composing row from two screenshots: its assigned card under Tuesday on a
    screenshot naming Wren, its due card under Wednesday on one naming ``second``, or nobody.
    Its id and the two screenshots' SHA-256s, its destination kept as new work."""
    named = shown_by("Wren", "Tuesday 10/6/2026", ASSIGNED_TUESDAY)
    other = shown_by(second, "Wednesday 10/7/2026", DUE_WEDNESDAY)
    draft_id = read_review(house, named, other)
    [piece] = state_of(house, draft_id).pieces
    keep_destinations(house, draft_id, {piece.name: {"new": 1}})
    return draft_id, sha_of(named), sha_of(other)


def read_more(house: House, draft_id: str, answer: dict[str, object]) -> str:
    """Another screenshot added to the review and read; its SHA-256."""
    label = hashlib.sha256(repr(answer).encode()).hexdigest()[:12]
    png = f"synthetic-png-{label}".encode()
    house.drafts.add_images(house.student, draft_id, [NewImage(png=png, width=800, height=600)])
    sha = hashlib.sha256(png).hexdigest()
    house.ticks.at += 200.0
    snapshot = house.drafts.snapshot(house.student, draft_id)
    [image_id] = [i.image_id for i in snapshot.images if i.sha256 == sha]
    attempt = house.drafts.start_attempt(house.student, draft_id, [image_id])
    assert attempt is not None
    checked = homework_reading(answer)
    assert checked.reading is not None
    house.drafts.store_reading(
        house.student,
        draft_id,
        attempt_id=attempt.attempt_id,
        image_id=image_id,
        sha256=sha,
        reading=UploadedReading(
            checked.reading.image_kind,
            "v1b",
            "screenshot-reading/1",
            checked.reading.model_dump_json(),
        ),
        timing=TIMING,
    )
    house.drafts.end_attempt(house.student, draft_id, attempt.attempt_id)
    return sha


MERGED: Final = {
    "hers": ((("name", "hers"), ("missing", "confirmed")), ("first_use", "hers", WREN_FORM)),
    "misread": (
        (("name", "misread"), ("missing", "confirmed")),
        ("first_use", "misread", WREN_FORM),
    ),
    "no answer for the name": ((("missing", "confirmed"),), "open"),
    "no confirmation of the unnamed screenshot": ((("name", "hers"),), "open"),
    "the confirmation cleared by another unnamed screenshot": (
        (("name", "hers"), ("missing", "confirmed")),
        "open",
    ),
    "two different names": ((("name", "hers"), ("avery", "hers")), "names"),
}
"""Each merged row's stored name answers, and what its save gives: the acceptance's status,
answer and form, or the refusal."""


@pytest.mark.parametrize("case", list(MERGED))
def test_a_merged_row_of_a_named_and_an_unnamed_screenshot_saves_once_with_both_answers(
    house: House, case: str
) -> None:
    given, expected = MERGED[case]
    second = "Avery" if case == "two different names" else None
    draft_id, named, other = merged_review(house, second)
    [merged] = unit_digests(state_of(house, draft_id))
    subjects = {"name": WREN_FORM, "missing": "missing", "avery": AVERY_FORM}
    for which, answer in given:
        name_answered(house, draft_id, subjects[which], answer)
    names = {named: ("first_use", WREN_FORM), other: ("missing", None)}
    if second is not None:
        names[other] = ("first_use", AVERY_FORM)
    if case == "the confirmation cleared by another unnamed screenshot":
        more = shown_by(None, "Thursday 10/8/2026", homework_card(title="Lab Report"))
        names[read_more(house, draft_id, more)] = ("missing", None)
    stored = house.record.draft_for_save(house.student, draft_id)
    assert stored is not None
    kept = {key: value for key, (value, _) in stored.answers.items()}
    press = dataclasses.replace(press_of(house, draft_id, "6" * 32), names=names)
    press = dataclasses.replace(press, units=tuple(u for u in press.units if u == merged))
    before = world(house)

    outcome = save(house, press)

    if isinstance(expected, tuple):
        assert isinstance(outcome, Saved)
        assert form_recorded(house, press.acceptance_id) == expected
        assert acceptances(house) == [press.acceptance_id]
        assert len(assignments(house)) == 1
        assert forms_kept(house) == ([WREN_FORM] if expected[1] == "hers" else [])
        assert isinstance(save(house, press), Replayed)
        assert acceptances(house) == [press.acceptance_id]
    else:
        assert outcome == NotSaved(str(expected), press.units)
        assert world(house) == before
    if case == "the confirmation cleared by another unnamed screenshot":
        assert ("name", "missing") not in kept


# ------------------------------------------------------------------ the save's one instant


class Moving:
    """A clock the test moves, that notes whether each reading came inside the writer."""

    def __init__(self, at: datetime, inside: list[bool]) -> None:
        self.at = at
        self.inside = inside
        self.reads: list[bool] = []
        self._zone = ZoneInfo("America/New_York")

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    def now(self) -> datetime:
        self.reads.append(bool(self.inside))
        return self.at

    def today(self) -> date:
        msg = "the household's date comes from the save's own instant"
        raise AssertionError(msg)


def test_the_save_reads_the_clock_once_inside_the_writer_and_dates_everything_from_it(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = two_row_review(house)
    press = press_of(house, draft_id, "8" * 32)
    inside: list[bool] = []
    clock = Moving(OBSERVED_AT, inside)
    later = OBSERVED_AT + timedelta(hours=30)
    real = house.record.comparing_and_writing
    seen: dict[str, object] = {}

    @contextmanager
    def waiting() -> Iterator[None]:
        clock.at = later
        with real():
            inside.append(True)
            try:
                yield
            finally:
                inside.pop()

    original_state = screenshot_save.review_state
    original_keep = screenshot_save._keep

    def state_seen(*args: object, **kwargs: object) -> object:
        seen["review_state"] = kwargs["now"]
        return original_state(*args, **kwargs)  # type: ignore[arg-type]

    def keep_seen(*args: object) -> None:
        seen["keep"] = args[-2:]
        original_keep(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(house.record, "comparing_and_writing", waiting)
    monkeypatch.setattr(screenshot_save, "review_state", state_seen)
    monkeypatch.setattr(screenshot_save, "_keep", keep_seen)
    outcome = save_homework(house.record, house.drafts, press, clock=clock, household=clock)

    assert isinstance(outcome, Saved)
    assert clock.reads == [True]
    assert seen["review_state"] == later
    assert seen["keep"] == (later, later.astimezone(clock.zone).date())
    assert later.astimezone(clock.zone).date() != OBSERVED_AT.astimezone(clock.zone).date()


# ------------------------------------------------- what the page says Save does, and what it does


def page_of(
    house: House, draft_id: str, corrections: Mapping[str, Mapping[str, str | None]] | None = None
) -> screenshot_page.RowsShown:
    """The review's rows as its page shows them, with these corrections typed."""
    state = review_state(
        house.record,
        house.record.draft_for_save(house.student, draft_id),  # type: ignore[arg-type]
        now=OBSERVED_AT,
        corrections=corrections or {},
    )
    images = house.drafts.snapshot(house.student, draft_id).images
    return screenshot_page.rows_shown(
        state,
        places={image.sha256: n for n, image in enumerate(images, 1)},
        images=[],
        page_key=b"synthetic-page-key",
        version=1,
        progress_revision=0,
        acceptance="a" * 32,
        until="",
        corrections=corrections or {},
    )


def type_lines(shown: screenshot_page.RowsShown) -> list[str]:
    """Each line under a Type, in page order."""
    return [
        text
        for unit in shown.units
        for block in unit.blocks
        for card in block.cards
        if card.type is not None
        for text, _ in card.type.notes
    ]


def quiet_lines(shown: screenshot_page.RowsShown) -> list[tuple[str, ...]]:
    """Each destination's quiet lines of what saving does, in page order."""
    return [block.head.quiet for unit in shown.units for block in unit.blocks if block.chosen]


def kept(house: House, draft_id: str, *answers: tuple[str, str, str]) -> None:
    """Answers kept at the review's version, each bound to no correction: (kind, subject,
    value)."""
    version = house.drafts.snapshot(house.student, draft_id).version
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=version,
        answers=[AnswerToKeep(k, s, v, correction_digest_none(), ()) for k, s, v in answers],
    )


def saved_kinds(house: House) -> dict[str, str]:
    return {a.title: a.kind.value for a in house.record.all_assignments()}


def test_a_slot_corrected_to_a_task_title_saves_as_a_task(house: House) -> None:
    """The joined-slot probe, inverted: a Reading Log corrected to Permission slip joins the
    Permission slip on one new slot, both suggest a task, and the line, the preview and Save
    agree on it."""
    draft_id = read_review(
        house,
        screenshot(
            "Friday 8/21/2026",
            card("Permission slip", "Sign it.", 200),
            card("Reading Log", "Read it.", 300),
        ),
    )
    log = next(p for p in state_of(house, draft_id).pieces if p.title == "Reading Log")
    corrections = {log.key: {"title": "Permission slip"}}
    press = answered_press(house, draft_id, corrections)
    shown = page_of(house, draft_id, corrections)
    outcome = save(house, press)

    assert type_lines(shown) == [
        "Saving now: a task, suggested from the title.",
        "Saving now: a task, suggested from the title.",
    ]
    assert quiet_lines(shown) == [
        ("No assigned date.", "New assignment. Saved as a task, as suggested.")
    ]
    assert isinstance(outcome, Saved)
    assert saved_kinds(house) == {"Permission slip": "TASK"}


def test_a_class_edit_keeps_a_new_slot_s_date_and_kind_answers(house: House) -> None:
    """Two rows of one name joined on a new slot, its due date and its kind asked and answered:
    a class edit of both rows keeps both answers, which name the slot and its pieces, and Save
    writes them."""
    draft_id = read_review(
        house,
        screenshot("Friday 8/21/2026", card("Reading Log", "Read it.", 200)),
        screenshot("Monday 8/24/2026", card("Reading Log", "Read it.", 200)),
    )
    a, b = sorted(state_of(house, draft_id).pieces, key=lambda p: p.due or MONDAY)
    kept(
        house,
        draft_id,
        ("destination", a.name, '{"new":1}'),
        ("destination", b.name, '{"new":1}'),
        ("kind", a.key, "TASK"),
        ("kind", b.key, "HOMEWORK"),
    )
    joined = state_of(house, draft_id)
    [dest] = joined.validation.destinations
    subject = screenshot_review.subject_for(dest, [a.name, b.name])
    kept(house, draft_id, ("due", subject, MONDAY.isoformat()), ("kind", subject, "TASK"))
    corrections = {key: {"course": "English 8"} for key in (a.key, b.key)}
    press = answered_press(house, draft_id, corrections)
    moved = review_state(
        house.record,
        house.record.draft_for_save(house.student, draft_id),  # type: ignore[arg-type]
        now=OBSERVED_AT,
        corrections=corrections,
    )
    outcome = save(house, press)

    [moved_dest] = moved.validation.destinations
    assert moved_dest != dest
    assert screenshot_review.subject_for(moved_dest, [a.name, b.name]) == subject
    assert all(unit.ready for unit in moved.validation.units)
    assert isinstance(outcome, Saved)
    [row] = house.record.all_assignments()
    assert (row.course, row.due_date, row.kind.value) == ("English 8", MONDAY, "TASK")


TYPE_CASES: Final = {
    "new, unchosen": (None, None, "Saving now: homework, suggested from the title.", "HOMEWORK"),
    "existing, unchosen": ("TASK", None, "Saving now: a task, as saved.", "TASK"),
    "new, her choice": (None, "TASK", "Saving now: a task, your choice.", "TASK"),
    "existing, her choice": ("TASK", "TASK", "Saving now: a task, your choice.", "TASK"),
}
"""Each Type line Save's rule gives one card: the kind of a saved assignment it goes to (None
for a new one), her choice on its row (None for none), the line, and the kind Save writes."""


@pytest.mark.parametrize("case", list(TYPE_CASES))
def test_each_type_line_is_save_s_rule(house: House, case: str) -> None:
    saved_kind, chosen, line, written = TYPE_CASES[case]
    destinations = None
    if saved_kind is not None:
        house.record.upsert_assignments(
            [
                Assignment(
                    assignment_id="assignment-reading-log-s1",
                    course=COURSE,
                    title="Reading Log",
                    due_date=FRIDAY,
                    dependencies=[],
                    reported_submission_status="not_reported",
                    assigned_on=None,
                    kind=AssignmentKind(saved_kind),
                )
            ]
        )
        destinations = {"Reading Log": {"existing": "assignment-reading-log-s1"}}
    draft_id = read_review(
        house, screenshot("Friday 8/21/2026", card("Reading Log", "Read pages 1 to 3.", 200))
    )
    [piece] = state_of(house, draft_id).pieces
    if chosen is not None:
        kept(house, draft_id, ("kind", piece.key, chosen))
    press = answered_press(house, draft_id, {}, destinations=destinations)
    shown = page_of(house, draft_id)
    outcome = save(house, press)

    assert type_lines(shown) == [line]
    assert isinstance(outcome, Saved)
    assert saved_kinds(house) == {"Reading Log": written}


def joined_review(house: House) -> tuple[str, Piece, Piece]:
    """Two cards of Reading Log, due Friday and Monday, both on one new slot."""
    draft_id = read_review(
        house,
        screenshot("Friday 8/21/2026", card("Reading Log", "Read it.", 200)),
        screenshot("Monday 8/24/2026", card("Reading Log", "Read it.", 200)),
    )
    a, b = sorted(state_of(house, draft_id).pieces, key=lambda p: p.due or MONDAY)
    kept(
        house,
        draft_id,
        ("destination", a.name, '{"new":1}'),
        ("destination", b.name, '{"new":1}'),
    )
    return draft_id, a, b


def test_a_choice_on_another_card_going_there_is_the_line_save_follows(house: House) -> None:
    draft_id, a, b = joined_review(house)
    kept(house, draft_id, ("kind", a.key, "TASK"))
    [dest] = state_of(house, draft_id).validation.destinations
    subject = screenshot_review.subject_for(dest, [a.name, b.name])
    kept(house, draft_id, ("due", subject, FRIDAY.isoformat()))
    shown = page_of(house, draft_id)
    outcome = save(house, answered_press(house, draft_id, {}))

    assert type_lines(shown) == [
        "Saving now: a task, your choice.",
        "Saving now: a task, chosen for Reading Log (screenshot 1, card 1).",
    ]
    assert quiet_lines(shown) == [("No assigned date.", "New assignment. Saved as a task.")]
    assert isinstance(outcome, Saved)
    assert saved_kinds(house) == {"Reading Log": "TASK"}


def test_a_kind_question_waiting_or_no_destination_is_the_line(house: House) -> None:
    """Choices that disagree ask the destination's own question, and the line links to it; a
    card with no destination yet waits for one."""
    draft_id, a, b = joined_review(house)
    kept(house, draft_id, ("kind", a.key, "TASK"), ("kind", b.key, "HOMEWORK"))
    asked = page_of(house, draft_id)
    fields = {
        q.field: q.anchor for unit in asked.units for block in unit.blocks for q in block.questions
    }
    links = [
        (text, target)
        for unit in asked.units
        for block in unit.blocks
        for card in block.cards
        if card.type is not None
        for text, target in card.type.notes
    ]
    lone = read_review(house, screenshot("Friday 8/21/2026", card("Vocab Quiz", "Study.", 200)))
    [piece] = state_of(house, lone).pieces
    kept(house, lone, ("destination", piece.name, '{"existing":"assignment-gone-x1"}'))

    [(text, target)] = set(links)
    assert text.startswith("Answer “Homework or task for A new Reading Log")
    assert text.endswith("?” first.")
    assert target in fields.values()
    assert type_lines(page_of(house, lone)) == ["Choose where it goes first."]


def test_a_split_row_s_type_line_names_each_piece_by_its_date(house: House) -> None:
    """One assigned card read under two day headings, answered "These are different
    assignments": each piece's line, led by its date, as Save writes each destination."""
    house.record.upsert_assignments(
        [
            Assignment(
                assignment_id="assignment-reading-log-t1",
                course=COURSE,
                title="Reading Log",
                due_date=MONDAY,
                dependencies=[],
                reported_submission_status="not_reported",
                assigned_on=date(2026, 8, 20),
                kind=AssignmentKind.TASK,
            )
        ]
    )
    log = homework_card(
        badge="ASSIGNED",
        course=COURSE,
        title="Reading Log",
        instruction="Read it.",
        footer="Due (8/24/2026)",
        y_top=200,
        y_bottom=280,
    )
    draft_id = read_review(
        house,
        homework_answer(
            headings=[{"text": "Thursday 8/20/2026", "y": 150, "uncertain": None}], cards=[log]
        ),
        homework_answer(
            headings=[{"text": "Friday 8/21/2026", "y": 150, "uncertain": None}], cards=[log]
        ),
    )
    [row] = state_of(house, draft_id).asking
    kept(house, draft_id, ("split", row.key, json.dumps("split")))
    thursday, friday = sorted(state_of(house, draft_id).pieces, key=lambda p: p.name)
    kept(
        house,
        draft_id,
        ("destination", thursday.name, '{"existing":"assignment-reading-log-t1"}'),
        ("destination", friday.name, '{"new":1}'),
    )
    shown = page_of(house, draft_id)

    assert type_lines(shown) == [
        "Thu 8/20: Saving now: a task, as saved.",
        "Fri 8/21: Saving now: homework, suggested from the title.",
    ]
    assert sorted(quiet_lines(shown)) == sorted(
        [
            ("Assigned Thursday, August 20.", "Updates Reading Log. Stays a task, as saved."),
            ("Assigned Friday, August 21.", "New assignment. Saved as homework, as suggested."),
        ]
    )
    outcome = save(house, press_of(house, draft_id))
    kinds = {a.assignment_id: a.kind.value for a in house.record.all_assignments()}

    assert isinstance(outcome, Saved)
    assert kinds.pop("assignment-reading-log-t1") == "TASK"
    assert list(kinds.values()) == ["HOMEWORK"]


def test_an_existing_assignment_chosen_with_its_kind_shows_nothing_new_and_no_kind_line(
    house: House,
) -> None:
    shot = screenshot("Friday 8/21/2026", card("Reading Log", "Read pages 1 to 3.", 200))
    first = read_review(house, shot)
    assert isinstance(save(house, answered_press(house, first, {})), Saved)
    draft_id = read_review(house, shot, homework_answer(cards=[]))
    [piece] = state_of(house, draft_id).pieces
    kept(house, draft_id, ("kind", piece.key, "HOMEWORK"))
    shown = page_of(house, draft_id)

    assert quiet_lines(shown) == [("No assigned date.", screenshot_page.SAID["already"])]
    assert not any("Saved as" in line or "Stays" in line for q in quiet_lines(shown) for line in q)


def test_no_assigned_date_on_a_saved_assignment_keeps_its_date(house: House) -> None:
    """ "No assigned date was given" on a card going to a saved assignment that has one: no
    question, nothing cleared, and the preview says the saved date stays."""
    house.record.upsert_assignments(
        [
            Assignment(
                assignment_id="assignment-reading-log-a1",
                course=COURSE,
                title="Reading Log",
                due_date=FRIDAY,
                dependencies=[],
                reported_submission_status="not_reported",
                assigned_on=date(2026, 8, 17),
                kind=AssignmentKind.HOMEWORK,
            )
        ]
    )
    log = homework_card(
        badge="ASSIGNED",
        course=COURSE,
        title="Reading Log",
        instruction="Read it.",
        footer="Due (8/21/2026)",
        y_top=200,
        y_bottom=280,
    )
    draft_id = read_review(
        house,
        homework_answer(
            headings=[{"text": "Wednesday 8/19/2026", "y": 150, "uncertain": None}], cards=[log]
        ),
    )
    [piece] = state_of(house, draft_id).pieces
    none = {dates_key(piece.name): {"assigned": None}}
    on_it = {"Reading Log": {"existing": "assignment-reading-log-a1"}}
    asked_without = page_of(house, draft_id)
    press = answered_press(house, draft_id, none, destinations=on_it)
    shown = page_of(house, draft_id, none)
    state = review_state(
        house.record,
        house.record.draft_for_save(house.student, draft_id),  # type: ignore[arg-type]
        now=OBSERVED_AT,
        corrections=none,
    )
    outcome = save(house, press)

    assert piece.assigned == date(2026, 8, 19)
    assert [u.questions for u in state.validation.units] == [()]
    assert quiet_lines(shown)[0] == (
        "Assigned Monday, August 17, as saved.",
        "Updates Reading Log. Stays homework, as saved.",
    )
    assert asked_without.units
    assert isinstance(outcome, Saved)
    [row] = house.record.all_assignments()
    assert row.assigned_on == date(2026, 8, 17)


def test_a_piece_s_digest_is_its_row_s_corrections_and_its_own_dates() -> None:
    """A destination answer's digest covers its row's fields and its own piece's dates, never
    another piece's; with no correction it is the digest of nothing, and saved progress of
    shape 3 digests as it always has."""
    nothing = hashlib.sha256(b"").hexdigest()[:12]
    row_fields = {"title": "Vocab Test"}
    own = screenshot_review.Piece("k|2026-08-21", "k", COURSE, "Q", FRIDAY, None, frozenset())
    sibling = dataclasses.replace(own, name="k|2026-08-24")
    typed = {"k": row_fields, dates_key(sibling.name): {"due": "2026-08-25"}}

    assert correction_digest(None) == correction_digest({}) == nothing
    assert piece_digest(own, {}) == nothing
    assert piece_digest(own, typed) == correction_digest(row_fields)
    assert piece_digest(sibling, typed) != piece_digest(own, typed)
    assert (
        correction_digest(row_fields)
        == hashlib.sha256(json.dumps(row_fields, separators=(",", ":")).encode()).hexdigest()[:12]
    )


def test_row_boxes_move_to_the_first_open_card_of_a_split_row(house: House) -> None:
    """A split row's class, title and instructions boxes sit on its first open card, and the post
    reader takes them from there. A row's pieces are saved together and a saved review is
    frozen, so a review never shows a split row with one piece saved; this test sets ``saved``
    on one piece directly to pin the rule on that defensive path."""
    log = homework_card(
        badge="ASSIGNED",
        course=COURSE,
        title="Reading Log",
        instruction="Read it.",
        footer="Due (8/24/2026)",
        y_top=200,
        y_bottom=280,
    )
    draft_id = read_review(
        house,
        homework_answer(
            headings=[{"text": "Thursday 8/20/2026", "y": 150, "uncertain": None}], cards=[log]
        ),
        homework_answer(
            headings=[{"text": "Friday 8/21/2026", "y": 150, "uncertain": None}], cards=[log]
        ),
    )
    [row] = state_of(house, draft_id).asking
    kept(house, draft_id, ("split", row.key, json.dumps("split")))
    state = state_of(house, draft_id)
    thursday, friday = sorted(state.pieces, key=lambda p: p.name)
    tables = house.record.draft_for_save(house.student, draft_id)
    assert tables is not None
    taken = review_state(
        house.record,
        tables._replace(saved={thursday.name: "assignment-reading-log-x1"}),
        now=OBSERVED_AT,
        corrections={},
    )

    def boxes(shown: screenshot_page.RowsShown) -> dict[str, list[str]]:
        return {
            card.name: [json.loads(b.subject)[1] for b in card.boxes]
            for unit in shown.units
            for block in unit.blocks
            for card in block.cards
        }

    def shown_for(review: ReviewState) -> screenshot_page.RowsShown:
        images = house.drafts.snapshot(house.student, draft_id).images
        return screenshot_page.rows_shown(
            review,
            places={image.sha256: n for n, image in enumerate(images, 1)},
            images=[],
            page_key=b"synthetic-page-key",
            version=1,
            progress_revision=0,
            acceptance="a" * 32,
            until="",
            corrections={},
        )

    row_fields = ["course", "title", "instruction:0", "due", "assigned"]
    assert boxes(shown_for(state)) == {thursday.name: row_fields, friday.name: ["due", "assigned"]}
    assert boxes(shown_for(taken)) == {friday.name: row_fields}
    reads = screenshot_save.fold_reads(taken)
    assert (row.key, "title") in reads
    assert (dates_key(thursday.name), "due") not in reads


@pytest.mark.parametrize("case", ["a lone card", "both cards of a joined slot"])
def test_a_card_corrected_to_a_task_title_saves_as_a_task(house: House, case: str) -> None:
    """A homework title corrected to a task's: the corrected title's suggestion is what the
    line says and Save writes, whichever card of a joined slot leads it."""
    cards = [card("Reading Log", "Read it.", 200)]
    if case != "a lone card":
        cards.append(card("Vocab Quiz", "Study it.", 300))
    draft_id = read_review(house, screenshot("Friday 8/21/2026", *cards))
    pieces = state_of(house, draft_id).pieces
    corrections = {p.key: {"title": "Permission slip"} for p in pieces}
    press = answered_press(house, draft_id, corrections)
    shown = page_of(house, draft_id, corrections)
    outcome = save(house, press)

    assert set(type_lines(shown)) == {"Saving now: a task, suggested from the title."}
    assert isinstance(outcome, Saved)
    assert saved_kinds(house) == {"Permission slip": "TASK"}


def test_a_new_slot_s_answers_stored_under_its_class_and_title_still_answer_it(
    house: House,
) -> None:
    """Date and kind answers of a joined new slot stored under its class and title answer its
    questions through the review's state, as answers named by its slot do; where both forms
    are stored, the slot's own form wins."""
    draft_id, a, b = joined_review(house)
    kept(house, draft_id, ("kind", a.key, "TASK"), ("kind", b.key, "HOMEWORK"))
    [dest] = state_of(house, draft_id).validation.destinations
    names = sorted([a.name, b.name])
    old = json.dumps([list(dest), names], separators=(",", ":"))
    new = screenshot_review.subject_for(dest, names)
    kept(house, draft_id, ("due", old, MONDAY.isoformat()), ("kind", old, "TASK"))
    stored_old = state_of(house, draft_id)
    kept(house, draft_id, ("due", new, FRIDAY.isoformat()))
    both = state_of(house, draft_id)

    assert old != new
    assert [u.ready for u in stored_old.validation.units] == [True]
    [entry] = stored_old.preview
    assert (entry.due, entry.written_kind, entry.open_questions) == (MONDAY, "TASK", ())
    [entry] = both.preview
    assert entry.due == FRIDAY
    outcome = save(house, press_of(house, draft_id))
    assert isinstance(outcome, Saved)
    [row] = house.record.all_assignments()
    assert (row.due_date, row.kind.value) == (FRIDAY, "TASK")


def test_a_card_s_source_line_says_the_title_as_read(house: House) -> None:
    """A card going to a saved assignment of another name says the title the screenshot read,
    never the one she typed, and a card going to its own name says nothing more."""
    house.record.upsert_assignments(
        [
            Assignment(
                assignment_id="assignment-word-study-r1",
                course=COURSE,
                title="Word Study",
                due_date=FRIDAY,
                dependencies=[],
                reported_submission_status="not_reported",
                assigned_on=None,
                kind=AssignmentKind.HOMEWORK,
            )
        ]
    )
    draft_id = read_review(
        house, screenshot("Friday 8/21/2026", card("Reading Log", "Read it.", 200))
    )
    [piece] = state_of(house, draft_id).pieces
    corrections = {piece.key: {"title": "Reading Notes"}}
    house.drafts.keep_answers(
        house.student,
        draft_id,
        version=house.drafts.snapshot(house.student, draft_id).version,
        answers=[
            AnswerToKeep(
                "destination",
                piece.name,
                '{"found":"assignment-word-study-r1"}',
                piece_digest(piece, corrections),
                (piece.name,),
            )
        ],
    )
    found = page_of(house, draft_id, corrections)
    plain = read_review(house, screenshot("Monday 8/24/2026", card("Vocab Quiz", "Study.", 200)))

    [card_shown] = [c for u in found.units for b in u.blocks for c in b.cards]
    assert card_shown.read_as == "read as Reading Log"
    [own] = [c for u in page_of(house, plain).units for b in u.blocks for c in b.cards]
    assert own.read_as is None
