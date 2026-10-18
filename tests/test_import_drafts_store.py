# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The stored import draft: its tables, its checks, completion, closing and the sweep.

Images here are opaque synthetic bytes, and readings are opaque JSON a fake rows function
reads. Acceptances are planted as a save would write them, so completion can be followed
without a save path.
"""

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

import pytest

from blossom.screenshots import homework_assembly, homework_rows
from blossom.stores.drafts import WriterBusy
from blossom.stores.gradebook import ADD_ACCEPTANCE
from blossom.stores.import_drafts import (
    DELETE_DRAFT,
    DRAFT_LIFETIME,
    MOST_IMAGES_PRESENT,
    READ_DEADLINE_SECONDS,
    AnswerToKeep,
    AttemptUnderWay,
    Carry,
    Closing,
    DraftClosed,
    DraftExpired,
    DraftFrozen,
    DraftNotFound,
    DraftRow,
    ImportDraftsStore,
    KindNotWired,
    NewImage,
    ProgressBehind,
    ReadTiming,
    ReviewChanged,
    StoredReading,
    TooManyImages,
    UnknownImage,
    UploadedReading,
    new_creation_token,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    OBSERVED_AT,
    SetClock,
    closed_world,
    homework_answer,
    homework_card,
    stored_homework,
)

HOMEWORK = "homework_screenshot"
GRADES = "grade_screenshot"
TIMING = ReadTiming(seconds=1.5, input_tokens=1200, output_tokens=300)
IMPORT_TABLES = frozenset(
    {
        "import_drafts",
        "import_draft_images",
        "import_draft_discards",
        "import_draft_progress",
        "import_draft_answers",
        "import_draft_marks",
    }
)
PINNED_DAY = date(2026, 3, 2)
"""A household date pinned far from the real clock, which expiry must never read."""


class Ticks:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


def fake_rows(readings: Sequence[StoredReading]) -> list[DraftRow]:
    """Every key each reading lists, numbered by the reading's place in what it is given, so
    a store that passed readings in arrival order would give different keys."""
    rows: list[DraftRow] = []
    for place, reading in enumerate(readings):
        body = json.loads(reading.body)
        rows.extend(DraftRow(key=f"{place}:{key}", readable=True) for key in body["rows"])
        rows.extend(DraftRow(key=f"{place}:{key}", readable=False) for key in body["unread"])
    return rows


def body(*keys: str, unread: Sequence[str] = (), name: str = "Wren") -> str:
    return json.dumps({"rows": list(keys), "unread": list(unread), "student": name})


def reading(*keys: str, unread: Sequence[str] = (), name: str = "Wren") -> UploadedReading:
    return UploadedReading(
        kind="homework_list",
        prompt_version="v1b",
        schema_version="screenshot-reading/1",
        body=body(*keys, unread=unread, name=name),
    )


def image(label: str) -> NewImage:
    return NewImage(png=f"synthetic-png-{label}".encode(), width=800, height=600)


def sha(label: str) -> str:
    return hashlib.sha256(f"synthetic-png-{label}".encode()).hexdigest()


class World:
    """One household file with her record, a draft store on it, and hands on its clocks."""

    def __init__(self, path: Path, **rows: object) -> None:
        self.path = path
        self.clock = SetClock(PINNED_DAY, OBSERVED_AT)
        self.ticks = Ticks()
        record = ProjectStateStore.open(path, self.clock)
        self.student = record.student_id()
        record.close()
        self.rows = rows or {HOMEWORK: fake_rows, GRADES: fake_rows}
        self.carries: dict[str, object] = {}
        self.pieces: dict[str, object] = {}
        self.store = self.reopen()

    def reopen(self) -> ImportDraftsStore:
        return ImportDraftsStore.open(
            self.path,
            self.clock,
            self.ticks,
            rows=self.rows,  # type: ignore[arg-type]
            carries=self.carries,  # type: ignore[arg-type]
            pieces=self.pieces,  # type: ignore[arg-type]
        )

    def draft(self, *labels: str, kind: str = HOMEWORK) -> str:
        added = self.store.create(
            self.student,
            kind=kind,
            made_by="parent",
            creation_token=new_creation_token(),
            images=[image(label) for label in labels],
        )
        assert added is not None
        return added.draft_id

    def image_ids(self, draft_id: str) -> dict[str, str]:
        snapshot = self.store.snapshot(self.student, draft_id)
        return {item.sha256: item.image_id for item in snapshot.images}

    def read(self, draft_id: str, label: str, *keys: str, **more: object) -> bool:
        """Start an attempt over one image, after any earlier attempt's deadline, and store
        its reading."""
        return self.read_as(draft_id, label, reading(*keys, **more))  # type: ignore[arg-type]

    def read_as(self, draft_id: str, label: str, uploaded: UploadedReading) -> bool:
        """Start an attempt over one image, after any earlier attempt's deadline, and store
        the reading given."""
        self.ticks.at += READ_DEADLINE_SECONDS
        image_id = self.image_ids(draft_id)[sha(label)]
        attempt = self.store.start_attempt(self.student, draft_id, [image_id])
        assert attempt is not None
        return self.store.store_reading(
            self.student,
            draft_id,
            attempt_id=attempt.attempt_id,
            image_id=image_id,
            sha256=sha(label),
            reading=uploaded,
            timing=TIMING,
        )

    def accept(self, draft_id: str, *keys: str, kind: str = HOMEWORK, replay: int = 0) -> None:
        """Plant an acceptance as a save of this draft writes it; ``replay`` numbers another
        copy of the same acceptance, as a save written again would leave."""
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(
                ADD_ACCEPTANCE,
                (
                    f"acceptance-{len(keys)}-{draft_id}-{keys}-{replay}",
                    self.student,
                    kind,
                    draft_id,
                    None,
                    None if kind == HOMEWORK else "class-synthetic",
                    None if kind == HOMEWORK else "Fall 2026",
                    json.dumps([[key, f"assignment-{key}"] for key in keys]),
                    "matches",
                    "shown",
                    None,
                    len(keys),
                    0,
                    0,
                    0,
                    len(keys),
                    0,
                    1,
                    OBSERVED_AT.isoformat(),
                    "parent",
                ),
            )
        connection.close()

    def outside_tables(self) -> dict[str, object]:
        """Every table, index and trigger of the file but the draft store's, rows included."""
        return closed_world([self.path], leaving_out=IMPORT_TABLES)

    def file_bytes(self) -> bytes:
        return self.path.read_bytes()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "blossom.sqlite3")


def states(world: World, draft_id: str) -> dict[str, str]:
    return {row.key: row.state for row in world.store.snapshot(world.student, draft_id).rows}


# ------------------------------------------------------------------ the store's pattern


def test_the_file_keeps_deleted_pages_overwritten_and_temporary_storage_in_memory(
    world: World,
) -> None:
    connection = world.store._connection
    assert connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2


def test_the_retention_policy_says_removed_from_the_file_and_never_erased() -> None:
    policy = ImportDraftsStore.retention_policy

    assert ImportDraftsStore.name == "import_drafts"
    assert "48 hours" in policy
    assert "removed from the file" in policy
    assert "erase" not in policy
    assert "progress saved for later" in policy
    assert "backup" in policy
    assert "by the first sweep after those 48 hours, or at the next start" in policy
    assert "From the instant a review is 48 hours old, nothing of it can be read." in policy
    assert "Unavailable and removed are two steps." in policy
    assert policy.startswith(
        "Keep a review's screenshots, what was read from them, the answers given on it, and "
        "any progress saved for later available until the review finishes or is discarded, "
        "and never past 48 hours from when it was started."
    )
    assert "its screenshots, readings, answers and progress are removed from the file" in policy
    assert "at the same 48 hours" not in policy


def test_an_expired_draft_is_unreadable_at_once_and_stays_in_the_file_until_a_sweep(
    world: World,
) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", name="Sentinel-Name-E6")
    world.store.save_progress(world.student, draft_id, revision=0, progress='{"typed": 1}')
    image_id = world.image_ids(draft_id)[sha("b")]
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME - timedelta(microseconds=1)
    assert world.store.snapshot(world.student, draft_id).images

    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME
    refused: list[Callable[[], object]] = [
        lambda: world.store.snapshot(world.student, draft_id),
        lambda: world.store.image_png(world.student, draft_id, image_id),
        lambda: world.store.add_images(world.student, draft_id, [image("c")]),
        lambda: world.store.remove_image(world.student, draft_id, image_id),
        lambda: world.store.start_attempt(world.student, draft_id, [image_id]),
        lambda: world.store.save_progress(world.student, draft_id, revision=1, progress="{}"),
        lambda: world.store.discard_rest(world.student, draft_id, version=2, keys=[]),
        lambda: world.store.discard_review(world.student, draft_id, version=2),
        lambda: world.store.clean_up(world.student, draft_id),
    ]
    for call in refused:
        with pytest.raises(DraftExpired):
            call()
    held = rows_in_file(world)
    in_file = b"Sentinel-Name-E6" in world.file_bytes()
    world.store.sweep()

    assert held == {"drafts": 1, "images": 2, "progress": 1}
    assert in_file
    assert rows_in_file(world) == {"drafts": 0, "images": 0, "progress": 0}
    assert b"Sentinel-Name-E6" not in world.file_bytes()


def rows_in_file(world: World) -> dict[str, int]:
    connection = sqlite3.connect(world.path)
    counts = {
        name: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        for name, table in (
            ("drafts", "import_drafts"),
            ("images", "import_draft_images"),
            ("progress", "import_draft_progress"),
        )
    }
    connection.close()
    return counts


def test_what_is_left_of_a_closed_draft_stays_in_the_file_until_a_sweep_after_expiry(
    world: World,
) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    version = world.store.snapshot(world.student, draft_id).version
    world.store.discard_rest(world.student, draft_id, version=version, keys=["0:r1", "0:r2"])
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME

    with pytest.raises(DraftExpired):
        world.store.snapshot(world.student, draft_id)
    connection = sqlite3.connect(world.path)
    left = [
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in ("import_drafts", "import_draft_discards")
    ]
    world.store.sweep()
    swept = [
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in ("import_drafts", "import_draft_discards")
    ]
    connection.close()

    assert left == [1, 2]
    assert swept == [0, 0]


def test_only_reading_an_image_reads_its_bytes(world: World) -> None:
    """Snapshots, uploads, Read, results, discards and the sweep work from the image rows
    without their PNGs; only the image read itself takes the bytes."""
    draft_id = world.draft("a", "b", "c")
    world.store.close()
    world.store = world.reopen()
    reads: list[str] = []

    def watch(action: int, table: str | None, column: str | None, *rest: object) -> int:
        if action == sqlite3.SQLITE_READ and table == "import_draft_images":
            reads.append(str(column))
        return sqlite3.SQLITE_OK

    world.store._connection.set_authorizer(watch)
    world.read(draft_id, "a", "r1", "r2")
    world.store.add_images(world.student, draft_id, [image("d")])
    world.store.remove_image(world.student, draft_id, world.image_ids(draft_id)[sha("d")])
    version = world.store.snapshot(world.student, draft_id).version
    world.store.discard_rest(world.student, draft_id, version=version, keys=["0:r1"])
    world.store.save_progress(world.student, draft_id, revision=0, progress="{}")
    world.store.clean_up(world.student, draft_id)
    world.store.sweep()
    assert "sha256" in reads
    assert "png" not in reads
    before = len(reads)

    world.store.image_png(world.student, draft_id, world.image_ids(draft_id)[sha("a")])

    assert "png" in reads[before:]


def test_a_draft_starts_at_version_one_with_its_fixed_expiry(world: World) -> None:
    draft_id = world.draft("a", "b")
    snapshot = world.store.snapshot(world.student, draft_id)

    assert len(draft_id) == 32
    assert snapshot.version == 1
    assert snapshot.expires_at == OBSERVED_AT + DRAFT_LIFETIME
    assert [item.sha256 for item in snapshot.images] == [sha("a"), sha("b")]
    assert world.store.image_png(world.student, draft_id, snapshot.images[0].image_id) == (
        b"synthetic-png-a"
    )


# ------------------------------------------------------------------ the rows function


def test_finished_closing_and_the_sweep_follow_the_rows_functions_keys(world: World) -> None:
    draft_id = world.draft("a")
    assert world.read(draft_id, "a", "r1", "r2")
    assert states(world, draft_id) == {"0:r1": "open", "0:r2": "open"}

    world.accept(draft_id, "0:r1")
    assert states(world, draft_id) == {"0:r1": "saved", "0:r2": "open"}
    assert world.store.clean_up(world.student, draft_id) is False

    world.accept(draft_id, "0:r2")
    assert world.store.snapshot(world.student, draft_id).finished is True
    swept = world.store.sweep()

    assert (swept.expired, swept.closed) == (0, 1)
    snapshot = world.store.snapshot(world.student, draft_id)
    assert snapshot.closed_at is not None
    assert snapshot.images == ()
    assert snapshot.saved == frozenset({"0:r1", "0:r2"})


def test_the_same_readings_in_any_order_give_the_same_keys(world: World) -> None:
    first = world.draft("a", "b")
    world.read(first, "b", "late")
    world.read(first, "a", "early")
    second = world.draft("b", "a")
    world.read(second, "a", "early")
    world.read(second, "b", "late")

    assert states(world, first) == states(world, second)
    assert sorted(key.split(":", 1)[1] for key in states(world, first)) == ["early", "late"]


def test_a_kind_with_no_rows_function_makes_no_draft(tmp_path: Path) -> None:
    world = World(tmp_path / "blossom.sqlite3", homework_screenshot=fake_rows)

    with pytest.raises(KindNotWired):
        world.draft("a", kind=GRADES)

    connection = sqlite3.connect(world.path)
    assert connection.execute("SELECT count(*) FROM import_drafts").fetchone()[0] == 0
    connection.close()


def test_the_sweep_applies_expiry_alone_to_a_kind_with_no_rows_function(tmp_path: Path) -> None:
    world = World(tmp_path / "blossom.sqlite3")
    draft_id = world.draft("a", kind=GRADES)
    world.read(draft_id, "a", "r1")
    world.accept(draft_id, "0:r1", kind=GRADES)
    world.store.close()
    world.rows = {HOMEWORK: fake_rows}
    world.store = world.reopen()

    assert world.store.sweep().closed == 0
    assert world.store.snapshot(world.student, draft_id).closed_at is None

    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME
    assert world.store.sweep().expired == 1
    with pytest.raises(DraftNotFound):
        world.store.snapshot(world.student, draft_id)


# ------------------------------------------------------------------ the homework rows function


def uploaded(label: str, answer: object) -> UploadedReading:
    """A homework screenshot's validated reading, as the reader stores it."""
    stored = stored_homework(label, answer)
    return UploadedReading(
        kind=stored.kind,
        prompt_version=stored.prompt_version,
        schema_version=stored.schema_version,
        body=stored.body,
    )


def real_keys(*images: tuple[str, object]) -> list[str]:
    """The keys the homework rows function gives for the labeled answers, in key order."""
    return [row.key for row in homework_rows([stored_homework(*image) for image in images])]


@pytest.fixture
def homework(tmp_path: Path) -> World:
    """A household file whose homework drafts use the homework rows function itself."""
    return World(
        tmp_path / "blossom.sqlite3", homework_screenshot=homework_rows, grade_screenshot=fake_rows
    )


def due_on(day: str, title: str = "Vocab Quiz") -> dict[str, object]:
    """One screenshot of a DUE card under the day heading named."""
    card = homework_card(badge="DUE", title=title, footer=None, instruction=None)
    return homework_answer(headings=[{"text": day, "y": 150, "uncertain": None}], cards=[card])


def test_with_the_homework_function_finished_closing_and_the_sweep_follow_its_keys(
    homework: World,
) -> None:
    draft_id = homework.draft("a")
    assert homework.read_as(draft_id, "a", uploaded("a", homework_answer()))
    first, second = real_keys(("a", homework_answer()))
    assert states(homework, draft_id) == {first: "open", second: "open"}

    homework.accept(draft_id, first)
    assert states(homework, draft_id) == {first: "saved", second: "open"}
    assert homework.store.clean_up(homework.student, draft_id) is False

    homework.accept(draft_id, second)
    assert homework.store.snapshot(homework.student, draft_id).finished is True
    assert homework.store.sweep().closed == 1
    assert homework.store.snapshot(homework.student, draft_id).images == ()


def test_with_the_homework_function_the_same_readings_in_any_order_give_the_same_keys(
    homework: World,
) -> None:
    one, two = due_on("Thursday 10/8/2026"), due_on("Thursday 10/22/2026")
    forward = homework.draft("one", "two")
    homework.read_as(forward, "one", uploaded("one", one))
    homework.read_as(forward, "two", uploaded("two", two))
    backward = homework.draft("two", "one")
    homework.read_as(backward, "two", uploaded("two", two))
    homework.read_as(backward, "one", uploaded("one", one))

    assert states(homework, forward) == states(homework, backward)
    assert len(states(homework, forward)) == 2


def test_a_reading_of_another_kind_doesnt_hold_the_draft_open(homework: World) -> None:
    draft_id = homework.draft("list", "message")
    homework.read_as(draft_id, "list", uploaded("list", due_on("Monday 10/5/2026")))
    homework.read_as(
        draft_id, "message", uploaded("message", homework_answer(image_kind="unsupported"))
    )
    [key] = states(homework, draft_id)

    homework.accept(draft_id, key)

    assert homework.store.snapshot(homework.student, draft_id).finished is True


def test_a_saved_row_stays_saved_and_its_screenshots_stay_as_they_are(homework: World) -> None:
    """Vocab Quiz due 10/8 in image one and due 10/22 in image two, the 10/22 round saved. Adding
    an image, removing one and starting a Read are each refused as frozen, writing nothing: the
    10/22 round's key is there and saved after every attempt."""
    one, two = due_on("Thursday 10/8/2026"), due_on("Thursday 10/22/2026")
    draft_id = homework.draft("one", "two")
    homework.read_as(draft_id, "one", uploaded("one", one))
    homework.read_as(draft_id, "two", uploaded("two", two))
    later = next(
        key for key in real_keys(("one", one), ("two", two)) if key not in real_keys(("one", one))
    )
    homework.accept(draft_id, later)

    def still_saved(after: str) -> None:
        assert states(homework, draft_id).get(later) == "saved", f"after {after}"

    still_saved("the save")
    before = homework.store.snapshot(homework.student, draft_id)
    ids = homework.image_ids(draft_id)
    with pytest.raises(DraftFrozen):
        homework.store.add_images(homework.student, draft_id, [image("unrelated")])
    still_saved("an image refused")
    with pytest.raises(DraftFrozen):
        homework.store.remove_image(homework.student, draft_id, ids[sha("one")])
    still_saved("a removal refused")
    with pytest.raises(DraftFrozen):
        homework.store.start_attempt(homework.student, draft_id, list(ids.values()))
    still_saved("a Read refused")

    after = homework.store.snapshot(homework.student, draft_id)
    assert (after.version, after.images, after.readings) == (
        before.version,
        before.images,
        before.readings,
    )
    assert list(states(homework, draft_id).values()).count("saved") == 1


THURSDAY = {"text": "Thursday 10/8/2026", "y": 150, "uncertain": None}


def copies(*tops: int, badge: str = "DUE", title: str = "Vocab Quiz") -> dict[str, object]:
    """One screenshot under Thursday of a matching card at each top edge given."""
    card = homework_card(badge=badge, title=title, footer=None, instruction=None)
    cards = [dict(card, y_top=top, y_bottom=top + 80) for top in tops]
    return homework_answer(headings=[THURSDAY], cards=cards)


def open_draft(world: World, *images: tuple[str, object]) -> str:
    """A draft whose images, labeled, are read in the order given."""
    draft_id = world.draft(*(label for label, _ in images))
    for label, answer in images:
        world.read_as(draft_id, label, uploaded(label, answer))
    return draft_id


def finished(world: World, draft_id: str) -> bool:
    return world.store.snapshot(world.student, draft_id).finished


def test_of_two_matching_rows_a_save_and_its_replay_leave_the_other_open(
    homework: World,
) -> None:
    """Two DUE Vocab Quiz cards in one screenshot are two rows: saving one, and the same save
    written again, never marks the other handled or closes the draft."""
    draft_id = open_draft(homework, ("both", copies(200, 300)))
    first, second = real_keys(("both", copies(200, 300)))

    homework.accept(draft_id, first)
    homework.accept(draft_id, first, replay=1)

    assert states(homework, draft_id) == {first: "saved", second: "open"}
    assert not finished(homework, draft_id)


def test_of_two_matching_rows_a_discard_leaves_the_other_open(homework: World) -> None:
    draft_id = open_draft(homework, ("both", copies(200, 300)))
    first, second = real_keys(("both", copies(200, 300)))
    version = homework.store.snapshot(homework.student, draft_id).version

    homework.store.discard_rest(homework.student, draft_id, version=version, keys=[first])

    assert states(homework, draft_id) == {first: "discarded", second: "open"}
    assert not finished(homework, draft_id)


def test_two_matching_rows_keep_their_decisions_in_any_order_and_after_reopening(
    homework: World,
) -> None:
    """The first copy's row, which a screenshot of one copy also gives, is saved; read in
    either order and reopened, the save stays on its own key and the other row stays open
    until it is discarded, which finishes the draft."""
    both, one = ("both", copies(200, 300)), ("one", copies(200))
    [layer_0] = real_keys(one)
    [layer_1] = set(real_keys(both)) - {layer_0}
    drafts = [open_draft(homework, both, one), open_draft(homework, one, both)]
    for draft_id in drafts:
        assert states(homework, draft_id) == {layer_0: "open", layer_1: "open"}
        homework.accept(draft_id, layer_0)

    homework.store.close()
    homework.store = homework.reopen()

    for draft_id in drafts:
        assert states(homework, draft_id) == {layer_0: "saved", layer_1: "open"}
        assert not finished(homework, draft_id)
        version = homework.store.snapshot(homework.student, draft_id).version
        homework.store.discard_rest(homework.student, draft_id, version=version, keys=[layer_1])
        assert finished(homework, draft_id)


def under_days(*cards: tuple[str, str, str | None]) -> dict[str, object]:
    """One screenshot of Practice set cards, each (day heading, badge, due line), each card
    under its own heading."""
    headings, found = [], []
    for place, (day, badge, footer) in enumerate(cards):
        headings.append({"text": day, "y": 100 + 200 * place, "uncertain": None})
        card = homework_card(badge=badge, title="Practice set", footer=footer, instruction=None)
        found.append(dict(card, y_top=150 + 200 * place, y_bottom=230 + 200 * place))
    return homework_answer(headings=headings, cards=found)


DIFFERENT_DATES = {
    "two days of one screenshot": (
        ("Monday 10/5/2026", "DUE", None),
        ("Tuesday 10/6/2026", "DUE", None),
    ),
    "a due card and the next round's assigned card": (
        ("Thursday 10/8/2026", "DUE", None),
        ("Thursday 10/8/2026", "ASSIGNED", "Due (10/12/2026)"),
    ),
}


@pytest.mark.parametrize("dates", DIFFERENT_DATES)
@pytest.mark.parametrize("decision", ["save", "discard"])
def test_of_two_rows_with_different_dates_deciding_one_leaves_the_other_open(
    homework: World, dates: str, decision: str
) -> None:
    """Different dates stay separate: the two cards of one screenshot are two rows. Read with
    a recapture of the first in either order and reopened, saving or discarding one leaves the
    other open and the draft open."""
    both = ("both", under_days(*DIFFERENT_DATES[dates]))
    first_again = ("first-again", under_days(DIFFERENT_DATES[dates][0]))
    [first] = real_keys(first_again)
    [second] = set(real_keys(both)) - {first}
    drafts = [open_draft(homework, both, first_again), open_draft(homework, first_again, both)]
    for draft_id in drafts:
        assert states(homework, draft_id) == {first: "open", second: "open"}
        if decision == "save":
            homework.accept(draft_id, first)
        else:
            version = homework.store.snapshot(homework.student, draft_id).version
            homework.store.discard_rest(homework.student, draft_id, version=version, keys=[first])

    homework.store.close()
    homework.store = homework.reopen()

    done = "saved" if decision == "save" else "discarded"
    for draft_id in drafts:
        assert states(homework, draft_id) == {first: done, second: "open"}
        assert not finished(homework, draft_id)


def assigned_under(day: str) -> dict[str, object]:
    """One screenshot of an ASSIGNED Practice set due 10/12 under the day given."""
    return under_days((day, "ASSIGNED", "Due (10/12/2026)"))


@pytest.mark.parametrize("decision", ["save", "discard"])
def test_a_decided_row_stays_decided_after_a_recapture_and_reopens_with_another_assigned_date(
    homework: World, decision: str
) -> None:
    """A row assigned 10/5 is saved or discarded. Once saved, a recapture is refused as frozen
    and the row stays saved. Discarded, an identical recapture keeps it decided, and a
    screenshot of the same card under 10/6 makes a new open row that no decision covers."""
    first, keep_open = ("first", assigned_under("Monday 10/5/2026")), ("keep-open", copies(200))
    [decided] = real_keys(first)
    [other] = real_keys(keep_open)
    draft_id = open_draft(homework, first, keep_open)
    if decision == "save":
        homework.accept(draft_id, decided)
    else:
        version = homework.store.snapshot(homework.student, draft_id).version
        homework.store.discard_rest(homework.student, draft_id, version=version, keys=[decided])
    done = "saved" if decision == "save" else "discarded"
    if decision == "save":
        with pytest.raises(DraftFrozen):
            homework.store.add_images(homework.student, draft_id, [image("again")])
        assert states(homework, draft_id) == {decided: done, other: "open"}
        return

    homework.store.add_images(homework.student, draft_id, [image("again")])
    homework.read_as(draft_id, "again", uploaded("again", assigned_under("Monday 10/5/2026")))
    assert states(homework, draft_id) == {decided: done, other: "open"}

    homework.store.add_images(homework.student, draft_id, [image("other-day")])
    homework.read_as(
        draft_id, "other-day", uploaded("other-day", assigned_under("Tuesday 10/6/2026"))
    )
    reopened = set(states(homework, draft_id)) - {decided, other}

    assert len(reopened) == 1
    assert states(homework, draft_id) == dict.fromkeys(reopened | {other}, "open")
    assert not finished(homework, draft_id)


def test_discarding_one_item_never_handles_another(homework: World) -> None:
    """Two screenshots each show an ASSIGNED Practice set with no due line under Thursday:
    two items, and discarding one leaves the other open and the draft open."""
    undated = copies(200, badge="ASSIGNED", title="Practice set")
    draft_id = open_draft(homework, ("a", undated), ("b", undated))
    first, second = sorted(states(homework, draft_id))
    version = homework.store.snapshot(homework.student, draft_id).version

    homework.store.discard_rest(homework.student, draft_id, version=version, keys=[first])

    assert states(homework, draft_id) == {first: "discarded", second: "open"}
    assert not finished(homework, draft_id)


def test_a_read_again_that_changes_only_the_student_keeps_item_decisions(
    homework: World,
) -> None:
    """Image one shows two ASSIGNED cards with no due line; one item is discarded and progress
    naming the other is saved. Image one is removed, added again and read with only the
    selected student's name and note changed: the discard stays, and the progress names keys
    the draft still holds."""
    items = copies(200, 300, badge="ASSIGNED", title="Practice set")
    renamed = dict(items, active_student={"name": "Rowan", "uncertain": "partly hidden"})
    draft_id = open_draft(homework, ("one", items), ("two", copies(200)))
    [row] = real_keys(("two", copies(200)))
    first, second = sorted(set(states(homework, draft_id)) - {row})
    version = homework.store.snapshot(homework.student, draft_id).version
    homework.store.discard_rest(homework.student, draft_id, version=version, keys=[first])
    progress = json.dumps({second: "checked"})
    revision = homework.store.snapshot(homework.student, draft_id).progress_revision
    homework.store.save_progress(homework.student, draft_id, revision=revision, progress=progress)

    homework.store.remove_image(
        homework.student, draft_id, homework.image_ids(draft_id)[sha("one")]
    )
    homework.store.add_images(homework.student, draft_id, [image("one")])
    homework.read_as(draft_id, "one", uploaded("one", renamed))

    snapshot = homework.store.snapshot(homework.student, draft_id)
    assert states(homework, draft_id) == {first: "discarded", second: "open", row: "open"}
    assert snapshot.progress == progress
    assert not snapshot.finished


# ------------------------------------------------------------------ results the store drops


def attempt_over(world: World, draft_id: str, *labels: str) -> str:
    ids = world.image_ids(draft_id)
    attempt = world.store.start_attempt(world.student, draft_id, [ids[sha(x)] for x in labels])
    assert attempt is not None
    return attempt.attempt_id


def offer(world: World, draft_id: str, attempt_id: str, label: str, image_id: str = "") -> bool:
    return world.store.store_reading(
        world.student,
        draft_id,
        attempt_id=attempt_id,
        image_id=image_id or world.image_ids(draft_id)[sha(label)],
        sha256=sha(label),
        reading=reading("r1", name="Sentinel-Name-Q7"),
        timing=TIMING,
    )


def test_a_result_after_the_deadline_is_dropped_with_its_timing_alone_logged(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    draft_id = world.draft("a")
    attempt_id = attempt_over(world, draft_id, "a")
    world.ticks.at += READ_DEADLINE_SECONDS

    with caplog.at_level(logging.DEBUG):
        assert offer(world, draft_id, attempt_id, "a") is False

    left = world.store.snapshot(world.student, draft_id).images[0]
    assert (left.read, left.covered, left.in_ended_attempt) == (False, False, True)
    assert "Sentinel-Name-Q7" not in caplog.text
    assert "1200" in caplog.text


def test_a_result_after_expiry_is_dropped(world: World) -> None:
    draft_id = world.draft("a")
    attempt_id = attempt_over(world, draft_id, "a")
    image_id = world.image_ids(draft_id)[sha("a")]
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME

    assert offer(world, draft_id, attempt_id, "a", image_id) is False
    world.clock.at = OBSERVED_AT
    assert world.store.snapshot(world.student, draft_id).images[0].read is False


def test_a_result_for_a_removed_or_replaced_image_is_dropped(world: World) -> None:
    draft_id = world.draft("a")
    attempt_id = attempt_over(world, draft_id, "a")
    old_id = world.image_ids(draft_id)[sha("a")]
    world.store.remove_image(world.student, draft_id, old_id)
    world.store.add_images(world.student, draft_id, [image("a2")])

    assert offer(world, draft_id, attempt_id, "a", old_id) is False
    replaced = world.image_ids(draft_id)[sha("a2")]
    assert (
        world.store.store_reading(
            world.student,
            draft_id,
            attempt_id=attempt_id,
            image_id=replaced,
            sha256=sha("a"),
            reading=reading("r1"),
            timing=TIMING,
        )
        is False
    )
    assert world.store.snapshot(world.student, draft_id).images[0].read is False


def test_a_result_into_a_closed_draft_is_dropped(world: World) -> None:
    draft_id = world.draft("a", "b")
    attempt_id = attempt_over(world, draft_id, "a")
    image_id = world.image_ids(draft_id)[sha("a")]
    version = world.store.snapshot(world.student, draft_id).version
    world.store.discard_review(world.student, draft_id, version=version)

    assert offer(world, draft_id, attempt_id, "a", image_id) is False


def test_a_busy_file_drops_the_result_and_leaves_the_image_unread(world: World) -> None:
    draft_id = world.draft("a")
    attempt_id = attempt_over(world, draft_id, "a")
    image_id = world.image_ids(draft_id)[sha("a")]
    other = sqlite3.connect(world.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(WriterBusy):
            world.store.store_reading(
                world.student,
                draft_id,
                attempt_id=attempt_id,
                image_id=image_id,
                sha256=sha("a"),
                reading=reading("r1"),
                timing=TIMING,
                wait=0.05,
            )
    finally:
        other.execute("ROLLBACK")
        other.close()

    assert world.store.snapshot(world.student, draft_id).images[0].read is False


def test_a_stored_reading_is_never_replaced(world: World) -> None:
    draft_id = world.draft("a")
    assert world.read(draft_id, "a", "first")
    image_id = world.image_ids(draft_id)[sha("a")]
    world.ticks.at += READ_DEADLINE_SECONDS

    assert world.store.start_attempt(world.student, draft_id, [image_id]) is None
    assert states(world, draft_id) == {"0:first": "open"}


# ------------------------------------------------------------------ a removed image stays removed


def test_a_late_result_never_restores_a_removed_image(world: World) -> None:
    draft_id = world.draft("x", "y")
    tab_b_version = world.store.snapshot(world.student, draft_id).version
    attempt_id = attempt_over(world, draft_id, "x", "y")
    x_id = world.image_ids(draft_id)[sha("x")]
    assert offer(world, draft_id, attempt_id, "y")
    assert world.store.snapshot(world.student, draft_id).version > tab_b_version

    world.store.remove_image(world.student, draft_id, x_id)

    assert offer(world, draft_id, attempt_id, "x", x_id) is False
    assert sha("x") not in world.image_ids(draft_id)
    world.store.add_images(world.student, draft_id, [image("x")])
    again = world.store.snapshot(world.student, draft_id)
    readded = next(item for item in again.images if item.sha256 == sha("x"))
    assert readded.image_id != x_id
    assert (readded.read, readded.covered) == (False, False)
    assert offer(world, draft_id, attempt_id, "x", readded.image_id) is False
    assert offer(world, draft_id, attempt_id, "x", x_id) is False


def test_removing_an_image_deletes_a_reading_that_arrived_since_the_page_opened(
    world: World,
) -> None:
    draft_id = world.draft("x")
    x_id = world.image_ids(draft_id)[sha("x")]
    assert world.read(draft_id, "x", "r1", name="Sentinel-Name-R1")
    assert b"Sentinel-Name-R1" in world.file_bytes()

    world.store.remove_image(world.student, draft_id, x_id)

    connection = sqlite3.connect(world.path)
    assert connection.execute("SELECT count(*) FROM import_draft_images").fetchone()[0] == 0
    connection.close()
    assert b"Sentinel-Name-R1" not in world.file_bytes()


def test_removing_an_image_not_in_the_draft_is_refused(world: World) -> None:
    draft_id = world.draft("a")
    other = world.draft("b")

    with pytest.raises(UnknownImage):
        world.store.remove_image(world.student, draft_id, world.image_ids(other)[sha("b")])


# ------------------------------------------------------------------ one attempt at a time


def test_a_second_press_during_a_current_attempt_starts_nothing(world: World) -> None:
    draft_id = world.draft("a", "b")
    ids = list(world.image_ids(draft_id).values())
    first = world.store.start_attempt(world.student, draft_id, ids[:1])
    assert first is not None

    with pytest.raises(AttemptUnderWay) as under_way:
        world.store.start_attempt(world.student, draft_id, ids)

    assert under_way.value.ends == first.ends
    snapshot = world.store.snapshot(world.student, draft_id)
    assert [item.covered for item in snapshot.images] == [True, False]
    world.ticks.at = first.ends
    assert world.store.start_attempt(world.student, draft_id, ids) is not None


def test_an_attempt_covers_only_the_unread_images_it_names(world: World) -> None:
    draft_id = world.draft("a", "b", "c")
    world.read(draft_id, "a", "r1")
    ids = world.image_ids(draft_id)
    world.ticks.at += READ_DEADLINE_SECONDS

    attempt = world.store.start_attempt(world.student, draft_id, [ids[sha("a")], ids[sha("b")]])

    assert attempt is not None
    assert attempt.covered == (ids[sha("b")],)
    assert attempt.ends == world.ticks.at + READ_DEADLINE_SECONDS


def test_a_failure_leaves_the_image_open_and_it_can_be_read_again(world: World) -> None:
    draft_id = world.draft("a")
    attempt_id = attempt_over(world, draft_id, "a")
    image_id = world.image_ids(draft_id)[sha("a")]
    version = world.store.snapshot(world.student, draft_id).version

    assert world.store.store_failure(
        world.student,
        draft_id,
        attempt_id=attempt_id,
        image_id=image_id,
        sha256=sha("a"),
        failure="not_valid",
        timing=TIMING,
    )

    snapshot = world.store.snapshot(world.student, draft_id)
    assert (snapshot.images[0].failure, snapshot.version) == ("not_valid", version)
    world.ticks.at += READ_DEADLINE_SECONDS
    assert world.read(draft_id, "a", "r1")
    assert world.store.snapshot(world.student, draft_id).images[0].failure is None


# ------------------------------------------------------------------ a restart


def test_a_restart_clears_attempts_and_keeps_images_readings_and_the_version(
    world: World,
) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1")
    world.ticks.at += READ_DEADLINE_SECONDS
    attempt_id = attempt_over(world, draft_id, "b")
    before = world.store.snapshot(world.student, draft_id)
    assert [item.covered for item in before.images] == [False, True]
    world.store.close()

    world.store = world.reopen()
    assert world.store.clear_attempts() == 1

    after = world.store.snapshot(world.student, draft_id)
    assert after.version == before.version
    assert after.attempt_ends is None
    assert [(item.read, item.covered, item.in_ended_attempt) for item in after.images] == [
        (True, False, False),
        (False, False, False),
    ]
    assert offer(world, draft_id, attempt_id, "b") is False
    world.store.discard_rest(world.student, draft_id, version=before.version, keys=["0:r1"])


# ------------------------------------------------------------------ the version


def test_every_change_to_images_or_readings_raises_the_version_and_nothing_else_does(
    world: World,
) -> None:
    draft_id = world.draft("a", "b")
    versions = [world.store.snapshot(world.student, draft_id).version]
    world.store.add_images(world.student, draft_id, [image("c")])
    versions.append(world.store.snapshot(world.student, draft_id).version)
    world.read(draft_id, "a", "r1")
    versions.append(world.store.snapshot(world.student, draft_id).version)
    world.store.remove_image(world.student, draft_id, world.image_ids(draft_id)[sha("c")])
    versions.append(world.store.snapshot(world.student, draft_id).version)
    world.store.save_progress(world.student, draft_id, revision=0, progress="{}")
    world.ticks.at += READ_DEADLINE_SECONDS
    attempt_over(world, draft_id, "b")
    world.store.add_images(world.student, draft_id, [image("a")])
    versions.append(world.store.snapshot(world.student, draft_id).version)

    assert versions == [1, 2, 3, 4, 4]


def test_discard_from_a_page_behind_the_version_returns_the_review_and_writes_nothing(
    world: World,
) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1")
    page = world.store.snapshot(world.student, draft_id).version
    world.store.add_images(world.student, draft_id, [image("c")])

    with pytest.raises(ReviewChanged):
        world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r1"])
    with pytest.raises(ReviewChanged):
        world.store.discard_review(world.student, draft_id, version=page)

    assert states(world, draft_id) == {"0:r1": "open"}
    assert len(world.store.snapshot(world.student, draft_id).images) == 3


def test_upload_and_removal_from_a_page_behind_the_version_proceed(world: World) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1")

    world.store.add_images(world.student, draft_id, [image("c")])
    world.store.remove_image(world.student, draft_id, world.image_ids(draft_id)[sha("b")])

    assert set(world.image_ids(draft_id)) == {sha("a"), sha("c")}


def test_discard_the_rest_raises_the_version_when_it_removes_images_and_leaves_rows_open(
    world: World,
) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", "r2")
    page = world.store.snapshot(world.student, draft_id).version

    after = world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r1"]).version

    snapshot = world.store.snapshot(world.student, draft_id)
    assert after == snapshot.version > page
    assert snapshot.closed_at is None
    assert [item.sha256 for item in snapshot.images] == [sha("a")]
    assert states(world, draft_id) == {"0:r1": "discarded", "0:r2": "open"}
    with pytest.raises(ReviewChanged):
        world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r2"])
    with pytest.raises(ReviewChanged):
        world.store.discard_review(world.student, draft_id, version=page)


def test_a_discard_naming_a_row_the_draft_doesnt_hold_writes_nothing(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    version = world.store.snapshot(world.student, draft_id).version

    with pytest.raises(ReviewChanged):
        world.store.discard_rest(world.student, draft_id, version=version, keys=["0:other"])

    assert states(world, draft_id) == {"0:r1": "open"}


# ------------------------------------------------------------------ discard and cleanup first


def test_discarding_the_review_removes_every_image_and_raises_the_version(world: World) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1")
    page = world.store.snapshot(world.student, draft_id).version

    world.store.discard_review(world.student, draft_id, version=page)

    after = world.store.snapshot(world.student, draft_id)
    assert after.version > page
    assert after.images == ()
    assert after.closed_at is not None
    with pytest.raises(DraftClosed):
        world.store.discard_review(world.student, draft_id, version=after.version)


def test_cleanup_after_a_finishing_save_raises_the_version(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    page = world.store.snapshot(world.student, draft_id).version
    world.accept(draft_id, "0:r1")

    assert world.store.clean_up(world.student, draft_id) is True

    after = world.store.snapshot(world.student, draft_id)
    assert after.version > page
    assert after.closed_at is not None
    with pytest.raises(DraftClosed):
        world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r1"])


# ------------------------------------------------------------------ saved progress


def test_saved_progress_survives_a_restart_and_extends_nothing(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    before = world.store.snapshot(world.student, draft_id)

    revision = world.store.save_progress(
        world.student, draft_id, revision=0, progress='{"0:r1": {"title": "Typed"}}'
    )
    world.store.close()
    world.clock.at = OBSERVED_AT + timedelta(hours=40)
    world.store = world.reopen()

    after = world.store.snapshot(world.student, draft_id)
    assert (after.progress, after.progress_revision) == ('{"0:r1": {"title": "Typed"}}', revision)
    assert after.expires_at == before.expires_at
    assert after.version == before.version
    assert states(world, draft_id) == {"0:r1": "open"}


def test_progress_from_a_page_behind_its_revision_stores_nothing(world: World) -> None:
    draft_id = world.draft("a")
    world.store.save_progress(world.student, draft_id, revision=0, progress='{"tab": "one"}')

    with pytest.raises(ProgressBehind):
        world.store.save_progress(world.student, draft_id, revision=0, progress='{"tab": "two"}')

    assert world.store.snapshot(world.student, draft_id).progress == '{"tab": "one"}'


def test_progress_is_deleted_when_the_draft_closes_and_refused_after(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    world.store.save_progress(
        world.student, draft_id, revision=0, progress='{"typed": "Sentinel-Typed-K4"}'
    )
    world.accept(draft_id, "0:r1")

    world.store.clean_up(world.student, draft_id)

    snapshot = world.store.snapshot(world.student, draft_id)
    assert snapshot.progress is None
    assert b"Sentinel-Typed-K4" not in world.file_bytes()
    with pytest.raises(DraftClosed):
        world.store.save_progress(
            world.student, draft_id, revision=snapshot.progress_revision, progress="{}"
        )


def test_progress_on_a_finished_draft_is_refused(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    world.accept(draft_id, "0:r1")

    with pytest.raises(DraftClosed):
        world.store.save_progress(world.student, draft_id, revision=0, progress="{}")


# ------------------------------------------------------------------ closing and deletion


def test_the_last_save_closes_the_draft_and_its_name_text_goes_with_the_images(
    world: World,
) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", name="Sentinel-Name-Q7 (uncertain: Sentinel-Note-Z3)")
    world.ticks.at += READ_DEADLINE_SECONDS
    world.read(draft_id, "b", "r2")
    world.store.save_progress(world.student, draft_id, revision=0, progress="{}")
    held = world.file_bytes()
    assert b"Sentinel-Name-Q7" in held
    assert b"Sentinel-Note-Z3" in held
    page = world.store.snapshot(world.student, draft_id).version
    keys = sorted(states(world, draft_id))
    assert [key.split(":")[1] for key in keys] in (["r1", "r2"], ["r2", "r1"])
    world.accept(draft_id, *keys)

    assert world.store.clean_up(world.student, draft_id) is True

    closed = world.store.snapshot(world.student, draft_id)
    assert closed.version > page
    assert closed.closed_at is not None
    assert (closed.images, closed.readings, closed.progress) == ((), (), None)
    assert closed.saved == frozenset(keys)
    assert b"Sentinel-Name-Q7" not in world.file_bytes()
    assert b"Sentinel-Note-Z3" not in world.file_bytes()


def test_when_cleanup_doesnt_run_the_sweep_closes_the_finished_draft(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", name="Sentinel-Name-Q7")
    world.accept(draft_id, "0:r1")

    assert world.store.sweep().closed == 1

    assert world.store.snapshot(world.student, draft_id).closed_at is not None
    assert b"Sentinel-Name-Q7" not in world.file_bytes()


def test_the_name_text_is_gone_from_the_file_after_expiry(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", name="Sentinel-Name-Q7")
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME

    assert world.store.sweep().expired == 1

    assert b"Sentinel-Name-Q7" not in world.file_bytes()


def test_discard_the_rest_removes_unread_images_and_finishes_the_draft(world: World) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", "r2")
    world.accept(draft_id, "0:r1")
    page = world.store.snapshot(world.student, draft_id).version

    world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r2"])

    closed = world.store.snapshot(world.student, draft_id)
    assert closed.images == ()
    assert closed.closed_at is not None
    assert closed.version > page
    assert closed.saved == frozenset({"0:r1"})
    assert closed.discarded == frozenset({"0:r2"})


def test_discard_the_rest_skips_rows_already_saved(world: World) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", "r2")
    page = world.store.snapshot(world.student, draft_id).version
    world.accept(draft_id, "0:r1")

    world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r1", "0:r2"])

    closed = world.store.snapshot(world.student, draft_id)
    assert (closed.saved, closed.discarded) == (frozenset({"0:r1"}), frozenset({"0:r2"}))


# ------------------------------------------------------------------ closing invents nothing


@pytest.mark.parametrize("how", ["discard_review", "not_hers", "finishing_discard", "sweep"])
def test_closing_never_invents_a_saved_outcome(world: World, how: str) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    world.accept(world.draft("other"), "0:unrelated")
    page = world.store.snapshot(world.student, draft_id).version
    before = world.outside_tables()

    if how in {"discard_review", "not_hers"}:
        world.store.discard_review(world.student, draft_id, version=page)
    elif how == "finishing_discard":
        world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r1", "0:r2"])
    else:
        world.store.discard_rest(world.student, draft_id, version=page, keys=["0:r1"])
        world.store.close()
        world.rows = {HOMEWORK: fake_rows, GRADES: fake_rows}
        world.store = world.reopen()
        # The draft holds one open row; a saved discard of it, planted as a lost response
        # would leave it, finishes the draft for the sweep to close.
        connection = sqlite3.connect(world.path)
        with connection:
            connection.execute(
                "INSERT INTO import_draft_discards VALUES (?, '0:r2', ?)",
                (draft_id, OBSERVED_AT.isoformat()),
            )
        connection.close()
        assert world.store.sweep().closed == 1

    closed = world.store.snapshot(world.student, draft_id)
    assert world.outside_tables() == before
    assert closed.closed_at is not None
    assert closed.saved == frozenset()
    assert [row for row in closed.rows if row.state == "open"] == []


# ------------------------------------------------------------------ expiry


def test_the_draft_expires_48_hours_after_creation_on_the_real_clock(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    world.store.save_progress(world.student, draft_id, revision=0, progress="{}")
    world.clock.day = PINNED_DAY + timedelta(days=30)
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME - timedelta(seconds=1)
    assert world.store.snapshot(world.student, draft_id).expires_at == OBSERVED_AT + DRAFT_LIFETIME
    before = world.outside_tables()

    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME

    with pytest.raises(DraftExpired):
        world.store.snapshot(world.student, draft_id)
    with pytest.raises(DraftExpired):
        world.store.add_images(world.student, draft_id, [image("b")])
    with pytest.raises(DraftExpired):
        world.store.save_progress(world.student, draft_id, revision=1, progress="{}")
    assert world.store.sweep().expired == 1
    with pytest.raises(DraftNotFound):
        world.store.snapshot(world.student, draft_id)
    assert world.outside_tables() == before


# ------------------------------------------------------------------ another student's draft


def test_a_draft_of_another_student_answers_as_an_unknown_one(world: World) -> None:
    draft_id = world.draft("a")
    image_id = world.image_ids(draft_id)[sha("a")]
    connection = sqlite3.connect(world.path)
    with connection:
        connection.execute("UPDATE import_drafts SET student_id = 'someone-else'")
    connection.close()

    calls: list[Callable[[], object]] = [
        lambda: world.store.snapshot(world.student, draft_id),
        lambda: world.store.add_images(world.student, draft_id, [image("b")]),
        lambda: world.store.remove_image(world.student, draft_id, image_id),
        lambda: world.store.start_attempt(world.student, draft_id, [image_id]),
        lambda: world.store.image_png(world.student, draft_id, image_id),
        lambda: world.store.save_progress(world.student, draft_id, revision=0, progress="{}"),
        lambda: world.store.discard_rest(world.student, draft_id, version=1, keys=[]),
        lambda: world.store.discard_review(world.student, draft_id, version=1),
        lambda: world.store.clean_up(world.student, draft_id),
        lambda: world.store.snapshot(world.student, "0" * 32),
    ]
    for call in calls:
        with pytest.raises(DraftNotFound):
            call()


# ------------------------------------------------------------------ the store side of uploads


def test_a_resent_first_upload_lands_on_the_same_draft(world: World) -> None:
    token = new_creation_token()
    first = world.store.create(
        world.student, kind=HOMEWORK, made_by="parent", creation_token=token, images=[image("a")]
    )
    again = world.store.create(
        world.student, kind=HOMEWORK, made_by="parent", creation_token=token, images=[image("a")]
    )

    assert first is not None
    assert again is not None
    assert again.draft_id == first.draft_id
    assert (again.added, again.duplicates, again.version) == ((), (0,), 1)


def test_a_creation_token_of_a_draft_of_another_kind_answers_as_unknown(world: World) -> None:
    token = new_creation_token()
    world.store.create(
        world.student, kind=HOMEWORK, made_by="parent", creation_token=token, images=[image("a")]
    )

    with pytest.raises(DraftNotFound):
        world.store.create(
            world.student, kind=GRADES, made_by="parent", creation_token=token, images=[image("b")]
        )

    connection = sqlite3.connect(world.path)
    assert connection.execute("SELECT count(*) FROM import_draft_images").fetchone()[0] == 1
    connection.close()


def test_a_first_upload_with_no_image_makes_no_draft(world: World) -> None:
    made = world.store.create(
        world.student,
        kind=HOMEWORK,
        made_by="household",
        creation_token=new_creation_token(),
        images=[],
    )

    assert made is None
    connection = sqlite3.connect(world.path)
    assert connection.execute("SELECT count(*) FROM import_drafts").fetchone()[0] == 0
    connection.close()


def test_ten_images_present_at_most_and_duplicates_take_no_place(world: World) -> None:
    draft_id = world.draft(*[str(n) for n in range(9)])

    with pytest.raises(TooManyImages) as full:
        world.store.add_images(world.student, draft_id, [image("9"), image("10")])
    assert full.value.free == 1
    assert len(world.image_ids(draft_id)) == 9

    added = world.store.add_images(world.student, draft_id, [image("0"), image("9")])
    assert (len(added.added), added.duplicates, added.free) == (1, (0,), 0)
    assert len(world.image_ids(draft_id)) == MOST_IMAGES_PRESENT

    world.store.remove_image(world.student, draft_id, world.image_ids(draft_id)[sha("0")])
    assert world.store.add_images(world.student, draft_id, [image("10")]).free == 0


def test_adding_to_a_closed_or_finished_draft_is_refused_and_to_an_emptied_one_accepted(
    world: World,
) -> None:
    finished = world.draft("a")
    world.read(finished, "a", "r1")
    world.accept(finished, "0:r1")
    with pytest.raises(DraftClosed):
        world.store.add_images(world.student, finished, [image("b")])
    world.store.clean_up(world.student, finished)
    with pytest.raises(DraftClosed):
        world.store.add_images(world.student, finished, [image("b")])

    emptied = world.draft("c")
    world.store.remove_image(world.student, emptied, world.image_ids(emptied)[sha("c")])
    assert world.store.add_images(world.student, emptied, [image("d")]).added


def test_the_last_place_goes_to_one_upload_whichever_connection_commits_first(
    world: World,
) -> None:
    draft_id = world.draft(*[str(n) for n in range(9)])
    second = world.reopen()

    world.store.add_images(world.student, draft_id, [image("one")])
    with pytest.raises(TooManyImages) as full:
        second.add_images(world.student, draft_id, [image("other")])

    second.close()
    assert full.value.free == 0
    assert len(world.image_ids(draft_id)) == MOST_IMAGES_PRESENT


def test_a_grade_acceptance_naming_a_key_with_no_target_is_read(tmp_path: Path) -> None:
    """The grade save writes a key with a null target for the term and each category; such a
    draft stays readable, takes more screenshots, and isn't passed over by the sweep."""

    def term_only(readings: Sequence[StoredReading]) -> list[DraftRow]:
        return [DraftRow("term", True)]

    world = World(
        tmp_path / "blossom.sqlite3", homework_screenshot=fake_rows, grade_screenshot=term_only
    )
    draft_id = world.draft("a", kind=GRADES)
    connection = sqlite3.connect(world.path)
    with connection:
        connection.execute(
            ADD_ACCEPTANCE,
            (
                "acceptance-grade",
                world.student,
                GRADES,
                draft_id,
                None,
                "class-synthetic",
                "Fall 2026",
                '[["term", null]]',
                "matches",
                "shown",
                None,
                1,
                0,
                0,
                0,
                1,
                0,
                1,
                OBSERVED_AT.isoformat(),
                "parent",
            ),
        )
    connection.close()

    assert world.store.snapshot(world.student, draft_id).saved == frozenset({"term"})
    assert world.store.add_images(world.student, draft_id, [image("b")]) is not None
    assert world.store.sweep().passed_over == 0


def test_saved_and_discarded_rows_and_items_keep_their_keys_as_other_images_change(
    homework: World,
) -> None:
    """A dated row under Thursday is saved, and the card above Thursday, with no heading of its
    own in that screenshot, is an item and is discarded. A screenshot showing Wednesday with
    the same card, and a removal, are both refused as frozen: the saved row and the discarded
    item keep their keys and states."""
    top = homework_card(badge="DUE", title="Reading log", footer=None, instruction=None)
    top.update(y_top=20, y_bottom=100)
    quiz = homework_card(
        badge="DUE", course="Math 8", title="U1 Quiz 3", footer=None, instruction=None
    )
    quiz.update(y_top=300, y_bottom=380)
    thursday = homework_answer(
        headings=[{"text": "Thursday 10/8/2026", "y": 250, "uncertain": None}], cards=[top, quiz]
    )
    wednesday = homework_answer(
        headings=[{"text": "Wednesday 10/7/2026", "y": 150, "uncertain": None}],
        cards=[dict(top, y_top=200, y_bottom=280)],
    )
    draft_id = homework.draft("thu", "other")
    homework.read_as(draft_id, "thu", uploaded("thu", thursday))
    homework.read_as(draft_id, "other", uploaded("other", due_on("Monday 10/5/2026", "Lab Quiz")))
    alone = homework_assembly([stored_homework("thu", thursday)], now=OBSERVED_AT)
    [row] = alone.rows
    [item] = alone.unread
    homework.accept(draft_id, row.key)
    version = homework.store.snapshot(homework.student, draft_id).version
    homework.store.discard_rest(homework.student, draft_id, version=version, keys=[item.key])

    with pytest.raises(DraftFrozen):
        homework.store.add_images(homework.student, draft_id, [image("wed")])
    with pytest.raises(DraftFrozen):
        homework.store.remove_image(
            homework.student, draft_id, homework.image_ids(draft_id)[sha("other")]
        )
    assert wednesday["cards"]

    rows = {r.key: r for r in homework.store.snapshot(homework.student, draft_id).rows}
    assert (rows[row.key].state, rows[row.key].readable) == ("saved", True)
    assert (rows[item.key].state, rows[item.key].readable) == ("discarded", False)


# ------------------------------------------------------------------ the review's own calls


def test_ending_an_attempt_moves_only_its_own_deadline_to_now(world: World) -> None:
    """A press whose calls have all ended ends its attempt: the deadline moves to now, its id
    stays on the draft and its images, a result for it is then dropped, and a Read again starts
    at once. Another attempt's id, or an attempt past its deadline, changes nothing."""
    draft_id = world.draft("a", "b")
    ids = world.image_ids(draft_id)
    attempt = world.store.start_attempt(world.student, draft_id, [ids[sha("a")], ids[sha("b")]])
    assert attempt is not None
    version = world.store.snapshot(world.student, draft_id).version

    assert not world.store.end_attempt(world.student, draft_id, "0" * 32)
    with pytest.raises(AttemptUnderWay):
        world.store.start_attempt(world.student, draft_id, [ids[sha("b")]])

    assert world.store.end_attempt(world.student, draft_id, attempt.attempt_id)

    snapshot = world.store.snapshot(world.student, draft_id)
    assert snapshot.attempt_ends is None
    assert snapshot.version == version
    assert all(item.in_ended_attempt and not item.covered for item in snapshot.images)
    assert not world.store.store_reading(
        world.student,
        draft_id,
        attempt_id=attempt.attempt_id,
        image_id=ids[sha("a")],
        sha256=sha("a"),
        reading=reading("late"),
        timing=TIMING,
    )
    assert not world.store.end_attempt(world.student, draft_id, attempt.attempt_id)
    again = world.store.start_attempt(world.student, draft_id, [ids[sha("b")]])
    assert again is not None
    assert again.covered == (ids[sha("b")],)


def test_ending_an_attempt_of_a_draft_not_hers_or_not_open_changes_nothing(
    world: World,
) -> None:
    draft_id = world.draft("a")
    image_id = world.image_ids(draft_id)[sha("a")]
    attempt = world.store.start_attempt(world.student, draft_id, [image_id])
    assert attempt is not None
    closed = world.draft("c")
    closed_attempt = world.store.start_attempt(
        world.student, closed, [world.image_ids(closed)[sha("c")]]
    )
    assert closed_attempt is not None
    world.store.discard_review(
        world.student, closed, version=world.store.snapshot(world.student, closed).version
    )
    before = world.file_bytes()

    assert not world.store.end_attempt("another-student", draft_id, attempt.attempt_id)
    assert not world.store.end_attempt(world.student, "f" * 32, attempt.attempt_id)
    assert not world.store.end_attempt(world.student, closed, closed_attempt.attempt_id)
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME
    assert not world.store.end_attempt(world.student, draft_id, attempt.attempt_id)
    assert world.file_bytes() == before


def test_setting_aside_a_group_keeps_every_image_and_raises_the_version_once(world: World) -> None:
    """Set aside discards the keys it names, rows and items alike, and nothing else: the unread
    image stays, the version is raised once for the whole group, the other rows stay open."""
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", "r2", "r3", unread=["u1"])
    snapshot = world.store.snapshot(world.student, draft_id)

    after = world.store.set_aside(
        world.student, draft_id, version=snapshot.version, keys=["0:u1", "0:r3"]
    ).version

    assert after == snapshot.version + 1
    assert states(world, draft_id) == {
        "0:r1": "open",
        "0:r2": "open",
        "0:r3": "discarded",
        "0:u1": "discarded",
    }
    assert len(world.store.snapshot(world.student, draft_id).images) == 2
    assert world.store.set_aside(world.student, draft_id, version=after, keys=["0:r2"]) == (
        after + 1,
        False,
        False,
    )
    assert states(world, draft_id)["0:r2"] == "discarded"


def test_setting_aside_skips_a_saved_key_and_refuses_what_the_draft_doesnt_hold(
    world: World,
) -> None:
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", "r2")
    world.accept(draft_id, "0:r1")
    version = world.store.snapshot(world.student, draft_id).version

    assert (
        world.store.set_aside(world.student, draft_id, version=version, keys=["0:r1"]).version
        == version + 1
    )
    version += 1
    assert states(world, draft_id)["0:r1"] == "saved"
    discards = world.store._connection.execute(
        "SELECT COUNT(*) FROM import_draft_discards WHERE draft_id = ?", (draft_id,)
    ).fetchone()[0]
    assert discards == 0

    with pytest.raises(ReviewChanged):
        world.store.set_aside(world.student, draft_id, version=version, keys=["0:r2", "0:nowhere"])
    with pytest.raises(ReviewChanged):
        world.store.set_aside(world.student, draft_id, version=version, keys=[])
    with pytest.raises(ReviewChanged):
        world.store.set_aside(world.student, draft_id, version=version - 1, keys=["0:r2"])
    with pytest.raises(DraftNotFound):
        world.store.set_aside("another-student", draft_id, version=version, keys=["0:r2"])
    assert states(world, draft_id)["0:r2"] == "open"

    world.store.discard_review(world.student, draft_id, version=version)
    closed_version = world.store.snapshot(world.student, draft_id).version
    with pytest.raises(DraftClosed):
        world.store.set_aside(world.student, draft_id, version=closed_version, keys=["0:r2"])


def test_setting_aside_the_last_open_row_finishes_and_closes_the_draft(world: World) -> None:
    """With one row saved and the other set aside, nothing is open: the same transaction
    deletes the images, readings and progress, sets the close and raises the version once, and
    writes nothing outside the draft's tables."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2", name="Sentinel-Name-S8")
    world.store.save_progress(world.student, draft_id, revision=0, progress='{"typed": 1}')
    world.accept(draft_id, "0:r1")
    version = world.store.snapshot(world.student, draft_id).version
    outside = world.outside_tables()

    after = world.store.set_aside(world.student, draft_id, version=version, keys=["0:r2"])

    assert after == (version + 2, True, True)  # the group's raise, and the close's
    snapshot = world.store.snapshot(world.student, draft_id)
    assert snapshot.closed_at is not None
    assert snapshot.images == ()
    assert snapshot.progress is None
    assert world.outside_tables() == outside
    assert b"Sentinel-Name-S8" not in world.file_bytes()


def test_setting_aside_after_expiry_is_refused(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    version = world.store.snapshot(world.student, draft_id).version
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME

    with pytest.raises(DraftExpired):
        world.store.set_aside(world.student, draft_id, version=version, keys=["0:r1"])


def test_open_drafts_lists_her_open_unexpired_reviews_in_the_order_made(world: World) -> None:
    """Open drafts of hers, with their kind, times and image count; closed, expired and another
    student's are left out."""
    first = world.draft("a", "b")
    world.clock.at = OBSERVED_AT + timedelta(minutes=5)
    second = world.draft("c", kind=GRADES)
    closed = world.draft("d")
    world.store.discard_review(
        world.student, closed, version=world.store.snapshot(world.student, closed).version
    )
    world.store.create(
        "another-student",
        kind=HOMEWORK,
        made_by="parent",
        creation_token=new_creation_token(),
        images=[image("e")],
    )

    found = world.store.open_drafts(world.student)

    assert [(item.draft_id, item.kind, item.images) for item in found] == [
        (first, HOMEWORK, 2),
        (second, GRADES, 1),
    ]
    assert found[0].created_at == OBSERVED_AT
    assert found[0].expires_at == OBSERVED_AT + DRAFT_LIFETIME
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME
    assert [item.draft_id for item in world.store.open_drafts(world.student)] == [second]
    world.clock.at = OBSERVED_AT + DRAFT_LIFETIME + timedelta(minutes=5)
    assert world.store.open_drafts(world.student) == ()


def test_open_drafts_reads_no_rows_and_no_acceptance(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A draft whose rows function fails and whose acceptance record can't be read is listed
    beside a good one, and nothing reaches a log: the list runs neither."""

    def failing(readings: Sequence[StoredReading]) -> list[DraftRow]:
        msg = "Sentinel-Rows-S6"
        raise RuntimeError(msg)

    world = World(tmp_path / "blossom.sqlite3", homework_screenshot=failing)
    bad = world.draft("a")
    connection = sqlite3.connect(world.path)
    with connection:
        connection.execute(
            "UPDATE import_draft_images SET reading = ?, reading_kind = 'homework_list', "
            "prompt_version = 'v1b', schema_version = 'screenshot-reading/1' WHERE draft_id = ?",
            (body("r1", name="Sentinel-Name-S6"), bad),
        )
        connection.execute(
            ADD_ACCEPTANCE,
            (
                "acceptance-unreadable",
                world.student,
                HOMEWORK,
                bad,
                None,
                None,
                None,
                "not json Sentinel-Accepted-S6",
                "matches",
                "shown",
                None,
                0,
                0,
                0,
                0,
                0,
                0,
                1,
                OBSERVED_AT.isoformat(),
                "parent",
            ),
        )
    connection.close()
    world.clock.at = OBSERVED_AT + timedelta(minutes=1)
    good = world.draft("b")
    caplog.set_level(logging.DEBUG)

    found = world.store.open_drafts(world.student)

    assert [item.draft_id for item in found] == [bad, good]
    assert "Sentinel" not in caplog.text


# ------------------------------------------------------------------ the review's answers and marks


def answer(
    subject: str, value: str = '{"new":1}', *names: str, kind: str = "destination"
) -> AnswerToKeep:
    return AnswerToKeep(kind, subject, value, "digest-none", names or (subject,))


def kept_answers(world: World, draft_id: str) -> dict[tuple[str, str], str]:
    return {
        (a.kind, a.subject): a.value
        for a in world.store.review_answers(world.student, draft_id).answers
    }


def test_answers_are_kept_at_the_pages_version_with_one_raise(world: World) -> None:
    """A press's answers are stored together with one version raise; the same answers again
    write nothing; a page made before them is refused, writing nothing."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    version = world.store.snapshot(world.student, draft_id).version

    kept = world.store.keep_answers(
        world.student,
        draft_id,
        version=version,
        answers=[answer("0:r1"), answer("0:r2", '{"new":2}')],
    )

    assert (kept.version, kept.stored, kept.dropped) == (version + 1, 2, 0)
    assert kept_answers(world, draft_id) == {
        ("destination", "0:r1"): '{"new":1}',
        ("destination", "0:r2"): '{"new":2}',
    }
    again = world.store.keep_answers(
        world.student,
        draft_id,
        version=kept.version,
        answers=[answer("0:r1"), answer("0:r2", '{"new":2}')],
    )
    assert (again.version, again.stored) == (kept.version, 0)
    with pytest.raises(ReviewChanged):
        world.store.keep_answers(
            world.student, draft_id, version=version, answers=[answer("0:r1", "{}")]
        )
    assert kept_answers(world, draft_id)[("destination", "0:r1")] == '{"new":1}'


def test_an_answer_for_pieces_another_save_accepted_is_dropped(world: World) -> None:
    """An answer naming only pieces another save took is dropped, never stored or refused; the
    rest of the post is stored."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    world.accept(draft_id, "0:r1")
    version = world.store.snapshot(world.student, draft_id).version

    kept = world.store.keep_answers(
        world.student, draft_id, version=version, answers=[answer("0:r1"), answer("0:r2")]
    )

    assert (kept.stored, kept.dropped) == (1, 1)
    assert set(kept_answers(world, draft_id)) == {("destination", "0:r2")}


def test_a_busy_writer_keeps_no_answer(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    version = world.store.snapshot(world.student, draft_id).version
    other = sqlite3.connect(world.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(WriterBusy):
            world.store.keep_answers(
                world.student, draft_id, version=version, answers=[answer("0:r1")], wait=0.05
            )
    finally:
        other.rollback()
        other.close()
    assert kept_answers(world, draft_id) == {}
    assert world.store.snapshot(world.student, draft_id).version == version


def test_saving_for_later_keeps_answers_and_progress_in_one_transaction(world: World) -> None:
    """On a current page both are stored; on a page behind another tab's save for later the
    answers are kept and no progress is stored; a write refused inside the transaction
    leaves neither."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    snapshot = world.store.snapshot(world.student, draft_id)

    first = world.store.save_for_later(
        world.student,
        draft_id,
        version=snapshot.version,
        progress_revision=snapshot.progress_revision,
        answers=[answer("0:r1")],
        progress='{"tab": "one"}',
    )
    assert (first.progress_saved, first.progress_revision, first.answers.stored) == (True, 1, 1)

    behind = world.store.save_for_later(
        world.student,
        draft_id,
        version=first.answers.version,
        progress_revision=0,
        answers=[answer("0:r2")],
        progress='{"tab": "two"}',
    )
    assert (behind.progress_saved, behind.progress_revision, behind.answers.stored) == (False, 1, 1)
    after = world.store.snapshot(world.student, draft_id)
    assert after.progress == '{"tab": "one"}'
    assert set(kept_answers(world, draft_id)) == {("destination", "0:r1"), ("destination", "0:r2")}

    def refuse_progress(action: int, table: str | None, *_: object) -> int:
        refused = table == "import_draft_progress" and action == sqlite3.SQLITE_INSERT
        return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK

    world.store._connection.set_authorizer(refuse_progress)
    with pytest.raises(sqlite3.DatabaseError):
        world.store.save_for_later(
            world.student,
            draft_id,
            version=behind.answers.version,
            progress_revision=1,
            answers=[answer("0:r1", '{"new":3}')],
            progress='{"tab": "three"}',
        )
    world.store._connection.set_authorizer(None)
    assert kept_answers(world, draft_id)[("destination", "0:r1")] == '{"new":1}'
    assert world.store.snapshot(world.student, draft_id).progress == '{"tab": "one"}'


def test_a_withdrawn_answer_is_deleted_with_the_press_s_answers_and_one_raise(
    world: World,
) -> None:
    """An answer the press left blank is deleted in the transaction that keeps the others; one
    never kept is passed over, and a press that changes nothing raises nothing."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    version = world.store.snapshot(world.student, draft_id).version
    confirm = answer('["r1","title"]', "as_read", "0:r1", kind="confirm")
    world.store.keep_answers(world.student, draft_id, version=version, answers=[confirm])

    kept = world.store.keep_answers(
        world.student,
        draft_id,
        version=version + 1,
        answers=[answer("0:r2")],
        withdrawn=[("confirm", '["r1","title"]'), ("instructions", "never kept")],
    )
    again = world.store.keep_answers(
        world.student,
        draft_id,
        version=version + 2,
        answers=[answer("0:r2")],
        withdrawn=[("confirm", '["r1","title"]')],
    )

    assert (kept.version, kept.stored, kept.withdrawn) == (version + 2, 1, 1)
    assert kept_answers(world, draft_id) == {("destination", "0:r2"): '{"new":1}'}
    assert (again.version, again.stored, again.withdrawn) == (version + 2, 0, 0)


def test_a_mark_stays_until_the_draft_closes_even_once_its_piece_is_answered(
    world: World,
) -> None:
    """A change's mark is its piece's only trace of the answers it cleared: answering the piece
    again keeps the mark, and only the close deletes it."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    world.store._connection.execute(
        "INSERT INTO import_draft_marks (draft_id, subject) VALUES (?, ?)", (draft_id, "0:r1")
    )
    world.store._connection.commit()
    version = world.store.snapshot(world.student, draft_id).version

    world.store.keep_answers(world.student, draft_id, version=version, answers=[answer("0:r1")])

    review = world.store.review_answers(world.student, draft_id)
    assert kept_answers(world, draft_id) == {("destination", "0:r1"): '{"new":1}'}
    assert review.marks == frozenset({"0:r1"})


def test_answers_and_marks_go_with_the_draft(world: World) -> None:
    """Closing deletes a draft's answers and marks in the close's transaction, and the expiry
    sweep's statements delete them with the rest."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    version = world.store.snapshot(world.student, draft_id).version
    world.store.keep_answers(world.student, draft_id, version=version, answers=[answer("0:r1")])
    world.store._connection.execute(
        "INSERT INTO import_draft_marks (draft_id, subject) VALUES (?, ?)", (draft_id, "0:r1")
    )
    world.store._connection.commit()

    world.store.discard_review(world.student, draft_id, version=version + 1)

    count = world.store._connection.execute(
        "SELECT (SELECT COUNT(*) FROM import_draft_answers), "
        "(SELECT COUNT(*) FROM import_draft_marks)"
    ).fetchone()
    assert tuple(count) == (0, 0)
    assert "DELETE FROM import_draft_answers WHERE draft_id = ?" in DELETE_DRAFT
    assert "DELETE FROM import_draft_marks WHERE draft_id = ?" in DELETE_DRAFT
    assert DELETE_DRAFT[-1] == "DELETE FROM import_drafts WHERE draft_id = ?"


def test_a_finished_draft_takes_no_request(world: World) -> None:
    """Every row saved, the draft finished but not yet closed: each request is refused as
    closed, and nothing is written."""
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1")
    world.accept(draft_id, "0:r1")
    snapshot = world.store.snapshot(world.student, draft_id)
    assert snapshot.finished
    assert snapshot.closed_at is None
    image_id = world.image_ids(draft_id)[sha("a")]

    for request in (
        lambda: world.store.remove_image(world.student, draft_id, image_id),
        lambda: world.store.image_png(world.student, draft_id, image_id),
        lambda: world.store.set_aside(
            world.student, draft_id, version=snapshot.version, keys=["0:r1"]
        ),
        lambda: world.store.keep_answers(
            world.student, draft_id, version=snapshot.version, answers=[]
        ),
        lambda: world.store.discard_review(world.student, draft_id, version=snapshot.version),
        lambda: world.store.start_attempt(world.student, draft_id, [image_id]),
    ):
        with pytest.raises(DraftClosed):
            request()
    assert world.store.snapshot(world.student, draft_id) == snapshot


def test_a_result_for_a_frozen_draft_is_dropped(world: World) -> None:
    """A Read under way when some homework is saved keeps nothing it returns."""
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1", "r2")
    world.ticks.at += READ_DEADLINE_SECONDS
    attempt_id = attempt_over(world, draft_id, "b")
    world.accept(draft_id, "0:r1")

    assert offer(world, draft_id, attempt_id, "b") is False
    assert not any(
        item.read
        for item in world.store.snapshot(world.student, draft_id).images
        if item.sha256 == sha("b")
    )


def test_a_split_row_is_saved_once_all_its_pieces_are(world: World) -> None:
    """With a pieces function, the acceptance names pieces only: the row stays open while one
    piece is saved, and is saved once both are."""

    def two_pieces(
        readings: Sequence[StoredReading], split: Mapping[str, str]
    ) -> dict[str, frozenset[str]]:
        return (
            {"0:r1": frozenset({"0:r1|mon", "0:r1|tue"})} if split.get("0:r1") == '"split"' else {}
        )

    world.pieces = {HOMEWORK: two_pieces}
    world.store = world.reopen()
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    version = world.store.snapshot(world.student, draft_id).version
    world.store.keep_answers(
        world.student,
        draft_id,
        version=version,
        answers=[AnswerToKeep("split", "0:r1", '"split"', "d", ())],
    )
    world.accept(draft_id, "0:r1|mon")
    assert states(world, draft_id)["0:r1"] == "open"

    world.accept(draft_id, "0:r1|tue")
    assert states(world, draft_id)["0:r1"] == "saved"


def test_a_change_of_readings_carries_inside_its_own_transaction(world: World) -> None:
    """The carry function's clears, marks and set-asides land with the removal that caused
    them; refused at the marks, the removal itself is taken back."""
    seen: list[tuple[int, int]] = []

    def carry(
        before: Sequence[StoredReading],
        after: Sequence[StoredReading],
        answers: Mapping[tuple[str, str], str],
        aside: frozenset[str],
    ) -> Carry:
        seen.append((len(before), len(after)))
        return Carry(
            delete=frozenset(answers), marks=frozenset({"0:r1"}), aside_insert=frozenset({"0:r9"})
        )

    world.carries = {HOMEWORK: carry}
    world.store = world.reopen()
    draft_id = world.draft("a", "b")
    world.read(draft_id, "a", "r1")
    world.read(draft_id, "b", "r9", "r8")
    version = world.store.snapshot(world.student, draft_id).version
    world.store.keep_answers(world.student, draft_id, version=version, answers=[answer("0:r1")])
    image_id = world.image_ids(draft_id)[sha("a")]

    def refuse_marks(action: int, table: str | None, *_: object) -> int:
        refused = table == "import_draft_marks" and action == sqlite3.SQLITE_INSERT
        return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK

    world.store._connection.set_authorizer(refuse_marks)
    with pytest.raises(sqlite3.DatabaseError):
        world.store.remove_image(world.student, draft_id, image_id)
    world.store._connection.set_authorizer(None)
    assert image_id in world.image_ids(draft_id).values()
    assert kept_answers(world, draft_id) != {}

    world.store.remove_image(world.student, draft_id, image_id)

    review = world.store.review_answers(world.student, draft_id)
    assert review.answers == ()
    assert review.marks == frozenset({"0:r1"})
    assert seen[-1] == (2, 1)


# ------------------------------------------------------------------ closing, decided inside


def test_not_hers_refuses_inside_its_transaction_once_anything_is_saved(world: World) -> None:
    draft_id = world.draft("a")
    world.read(draft_id, "a", "r1", "r2")
    page = world.store.snapshot(world.student, draft_id).version
    world.accept(draft_id, "0:r1")
    before = world.store.snapshot(world.student, draft_id)

    with pytest.raises(DraftFrozen):
        world.store.discard_review(world.student, draft_id, version=page, refuse_if_saved=True)
    after = world.store.snapshot(world.student, draft_id)
    rows = states(world, draft_id)
    discarded = world.store.discard_review(world.student, draft_id, version=page)

    assert after == before
    assert after.closed_at is None
    assert rows == {"0:r1": "saved", "0:r2": "open"}
    assert discarded == Closing(world.store.snapshot(world.student, draft_id).version, True, True)


def test_every_closing_call_returns_what_its_transaction_saw(world: World) -> None:
    def version_of(draft_id: str) -> int:
        return world.store.snapshot(world.student, draft_id).version

    page_of = version_of

    quiet = world.draft("a")
    world.read(quiet, "a", "r1", "r2")
    left = world.store.set_aside(world.student, quiet, version=page_of(quiet), keys=["0:r1"])
    after_leave = version_of(quiet)
    finished = world.store.discard_rest(world.student, quiet, version=page_of(quiet), keys=["0:r2"])

    raced = world.draft("b")
    world.read(raced, "b", "r1", "r2")
    page = page_of(raced)
    world.accept(raced, "0:r1")
    closed_by_leave = world.store.set_aside(world.student, raced, version=page, keys=["0:r2"])

    finishing = world.draft("c")
    world.read(finishing, "c", "r1", "r2")
    page = page_of(finishing)
    world.accept(finishing, "0:r1")
    closed_by_finish = world.store.discard_rest(
        world.student, finishing, version=page, keys=["0:r1", "0:r2"]
    )

    discarding = world.draft("d")
    world.read(discarding, "d", "r1")
    nothing_saved = world.store.discard_review(
        world.student, discarding, version=page_of(discarding)
    )

    assert left == Closing(after_leave, False, False)
    assert finished == Closing(version_of(quiet), True, False)
    assert closed_by_leave == Closing(version_of(raced), True, True)
    assert closed_by_finish == Closing(version_of(finishing), True, True)
    assert nothing_saved == Closing(version_of(discarding), True, False)
