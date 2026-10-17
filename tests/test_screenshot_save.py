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
from datetime import date
from typing import Final

import pytest

from blossom import intake
from blossom.grades.identity import name_form
from blossom.screenshot_review import carry, piece_names, unit_name, value_json
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
        now=OBSERVED_AT,
        today=OBSERVED_AT.date(),
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
    corrections: Mapping[str, Mapping[str, str]],
    confirmed: Sequence[tuple[str, str]] = (),
    *,
    acceptance: str = "c" * 32,
    rows: Sequence[str] | None = None,
    destinations: Mapping[str, Mapping[str, object]] | None = None,
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
            correction_digest(corrections.get(piece.key)),
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


COUNT_CASES: Final[dict[str, Callable[[House], tuple[tuple[object, ...], tuple[int, ...]]]]] = {
    "two corrected and three confirmed": fields_corrected_and_confirmed,
    "the read values typed back": read_values_typed_back,
    "an unreadable date confirmed": unreadable_date_confirmed,
    "a due date chosen among those read": due_date_chosen_among_those_read,
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
