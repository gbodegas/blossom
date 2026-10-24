# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The screenshot review as a parent reads it: each group of what saving adds as her week's
card, each card going there with its source, its corrections and its questions, and the
group's refusals and offers.

Everything here is worked out from the review's state, which the save works from too, so the
page and the save agree. A preselected destination is shown checked and is never stored until
a press posts it; a Type is never preselected, and the line under it says what saving does
with it as Save does. Every box of "Correct what was read" holds its field's whole current
value. Every sentence the review's rows can show is in this module, in one place.
"""

import dataclasses
import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Final

from blossom.grades.draft import folded
from blossom.school_instructions import to_wire
from blossom.screenshot_review import (
    KINDS,
    ChangeTold,
    Corrections,
    DateQuestion,
    Destination,
    Offer,
    Piece,
    PreviewEntry,
    Refusal,
    SavedRow,
    UnitCheck,
    Validation,
    dates_key,
    dest_key,
    dest_kind,
    facts,
    field_most,
    kind_question,
    offers,
    reference,
    settles_due,
    subject_for,
    typed_day,
    unit_name,
    usable_correction,
    value_json,
    value_of,
)
from blossom.screenshot_save import (
    ReviewState,
    box_texts,
    confirm_fields,
    confirm_subject,
    fold_answers,
    instruction_subject,
    read_digest,
    unit_digests,
    unreadable_dates,
    wordless_rows,
)
from blossom.screenshots import CardPlace, ScreenshotRow, UnreadCard

SAID: Final = {
    "status": (
        "Save review for later keeps your choices and your corrections. Keep these answers "
        "keeps your choices; where a card goes is asked again if you come back before saving "
        "its corrections. Save selected homework saves the ticked assignments with your "
        "corrections."
    ),
    "preselected": "Suggested choice. You can change it.",
    "not_saved_yet": "Not saved yet.",
    "find": "Find under another name\u2026",
    "no_words": "no instructions",
    "no_assigned": "no assigned date",
    "no_due": "no due date",
    "has_words": "Has this card's words",
    "lacks_words": "Doesn't have this card's words",
    "no_new_words": "No new words.",
    "nothing_new": "Nothing new is written.",
    "stays_open": "It stays open.",
    "choose_each": "Choose where each card goes.",
    "choose_again": "A choice here isn't offered any more; choose again.",
    "use_change": "Use this change",
    "leave": "Leave this group unsaved",
    "split": "These show different assigned days. Choose one:",
    "split_apart": "These are different assignments",
    "none_apply": "None of these apply",
    "confirm": "Confirm as read",
    "homework": "Homework",
    "task": "Task",
    "type": "Homework or task?",
    "type_helper": "Task: a form to sign, supplies to bring, or similar.",
    "due_differs": "Its due line and its day heading name different dates. Which due date does "
    "Blossom use?",
    "too_long": "Keep the {named} to {most:,} characters or fewer.",
    "nothing": "none",
    "lost_correction": "This card's corrections changed since you chose, so choose where it goes "
    "again.",
    "where": "Where it goes",
    "unsure": "The reader wasn't sure of the {named}.",
    "correct_it": "or correct it under",
    "fold": "Correct what was read",
    "said_fold": "What the screenshot said",
    "source": "From screenshot {shot}, card {card}",
    "read_as": "read as {title}",
    "view": "View screenshot",
    "due_hint": "Choose which due date applies above, or type the right one.",
    "assigned_hint": "Leave it empty to keep the date as read.",
    "no_assigned_given": "No assigned date was given",
    "both": "Choose an assigned date or No assigned date was given, not both.",
    "unreadable": "The date given was \u201c{value}\u201d, which is not a date Blossom can read.",
    "new_assignment": "New assignment.",
    "already": "Already saved. Nothing new is written.",
    "updates": "Updates {title}.",
    "no_assigned_line": "No assigned date.",
    "no_due_line": "No due date",
    "choose_first": "Choose where it goes first.",
    "kind_first": "Answer \u201cHomework or task for {label}?\u201d first.",
    "typing_lost": "Typed corrections aren't kept by this button; use Save review for later first.",
    "tick_one": "Save this assignment",
    "tick_many": "Save these assignments",
}
"""The fixed sentences and labels of the review's rows."""

NEEDED: Final = {
    "course": "A class is needed.",
    "title": "A title is needed.",
    "due": "A due date is needed.",
}
"""What a cleared box that can't be empty says beneath it."""

BOX_LABELS: Final = {
    "course": "Class",
    "title": "Title",
    "instruction": "Instructions",
    "due": "Due date",
    "assigned": "Assigned date",
}
"""Each box of "Correct what was read", by field."""

BOX_NAMED: Final = {
    "course": "class",
    "title": "title",
    "instruction": "instructions",
    "due": "due date",
    "assigned": "assigned date",
}
"""How a box's field is named in "Keep the {named} to {most} characters or fewer."."""

KIND_WORD: Final = {"HOMEWORK": "homework", "TASK": "a task"}
"""A kind as a line says it."""

TYPE_LINE: Final = {
    "suggested": "Saving now: {kind}, suggested from the title.",
    "saved": "Saving now: {kind}, as saved.",
    "chosen": "Saving now: {kind}, your choice.",
    "chosen_for": "Saving now: {kind}, chosen for {card}.",
}
"""The line under a row's Type, by where the kind Save writes comes from."""

KIND_LINE: Final = {
    "suggested": "Saved as {kind}, as suggested.",
    "saved": "Stays {kind}, as saved.",
    "chosen": "Saved as {kind}.",
}
"""The preview's kind line, by where the kind Save writes or keeps comes from."""

CHANGE_SAID: Final = {
    "cleared": "Your screenshots changed, so these rows' answers were cleared: {labels}. Answer "
    "them again.",
    "copies": "Blossom can't tell which of these identical cards is which after the change, so "
    "their answers were cleared.",
    "carried": "Your screenshots changed. {label} is still left unsaved, as you chose for these "
    "cards.",
    "asked": "Your screenshots changed. {label} now has cards you hadn't left unsaved, so it asks "
    "again. You can leave it unsaved again.",
    "name": "Your screenshots changed, so confirm again that these are her homework.",
}
"""What the page after a change of screenshots says of the answers it cleared, the rows left
unsaved it kept or asks about again, and a confirmation of her homework it cleared."""

OPEN_NAMES: Final = {
    "due": "the due date",
    "assigned": "the assigned date",
    "kind": "homework or task",
    "instructions": "which instructions apply",
    "confirm": "a field the reader wasn't sure of",
    "name": "the student name",
    "due_read": "which due date Blossom uses",
}
"""How the preview names each question still open."""

FIELD_NAMES: Final = {
    "badge": "due or assigned label",
    "course": "class",
    "title": "title",
    "instruction": "instructions",
    "footer": "due date",
    "heading": "assigned date (day heading)",
}
"""How an uncertain field is named in "The reader wasn't sure of the {field}."."""


def day_text(day: date | None) -> str:
    """A day as the review's rows say it: "Wed 10/14"."""
    return "" if day is None else f"{day:%a} {day.month}/{day.day}"


def long_day(day: date) -> str:
    """A day as her week's card says it: "Thursday, October 8"."""
    return f"{day:%A, %B} {day.day}"


def card_text(places: Mapping[str, int], card: CardPlace) -> str:
    """Where a card was read: "screenshot 2, card 1"."""
    return f"screenshot {places.get(card.sha256, 0)}, card {card.place + 1}"


def piece_text(places: Mapping[str, int], piece: Piece) -> str:
    """A piece as its legend and the preview name it, by its cards' places."""
    where = "; ".join(card_text(places, c) for c in piece.cards) or "a card"
    day = f", assigned {day_text(piece.assigned)}" if piece.split and piece.assigned else ""
    return f"{piece.title} ({where}{day})"


def change_said(told: ChangeTold) -> list[str]:
    """The sentences the page after a change of screenshots opens with: the rows whose answers
    it cleared, by title and, for a piece split by assigned day, that day; that identical cards
    can't be told apart, when any of those rows is one of them; each row left unsaved it kept
    or asks about again; and that her homework must be confirmed again, when it cleared that
    confirmation."""
    out = []
    if told.cleared:
        labels = "; ".join(
            row.title + (f", assigned {day_text(row.day)}" if row.day else "")
            for row in told.cleared
        )
        out.append(CHANGE_SAID["cleared"].format(labels=labels))
    if any(row.copies for row in told.cleared):
        out.append(CHANGE_SAID["copies"])
    out += [CHANGE_SAID["carried"].format(label=title) for title in told.carried]
    out += [CHANGE_SAID["asked"].format(label=title) for title in told.asked]
    if told.name_asked:
        out.append(CHANGE_SAID["name"])
    return out


def words_line(first: str | None, more: int) -> str:
    """Instructions as an entry and a card show them: the first, shortened, and how many
    more; "no instructions" when there are none."""
    if first is None:
        return SAID["no_words"]
    shown = first if len(first) <= 60 else first[:57] + "..."
    rest = f" and {more} more" if more else ""
    return f"\u201c{shown}\u201d{rest}"


def words_text(row: SavedRow) -> str:
    """An assignment's first instruction, shortened, and how many more."""
    return words_line(row.first_words, row.more_words)


def row_label(row: SavedRow) -> str:
    """An existing assignment as an entry reads before its reference."""
    due = f"due {day_text(row.due)}" if row.due else SAID["no_due"]
    assigned = f"assigned {day_text(row.assigned)}" if row.assigned else SAID["no_assigned"]
    return f"{row.title}, {due}, {assigned}, {words_text(row)}"


def labels_for(rows: Sequence[SavedRow], always: Iterable[str] = ()) -> dict[str, str]:
    """Each assignment's label, with its reference wherever two would read the same and for
    every assignment in ``always``, worked out among those that read the same."""
    plain = {r.assignment_id: row_label(r) for r in rows}
    always = frozenset(always)
    out = {}
    for row in rows:
        twins = [i for i, text in plain.items() if text == plain[row.assignment_id]]
        out[row.assignment_id] = (
            plain[row.assignment_id]
            if len(twins) == 1 and row.assignment_id not in always
            else f"{plain[row.assignment_id]}, {reference(row.assignment_id, twins)}"
        )
    return out


def page_labels(
    state: ReviewState, reworked: Iterable[tuple[Offer, Sequence[PreviewEntry]]] = ()
) -> dict[str, str]:
    """One label per assignment the review page names, used by every part of it: each open
    piece's entries and their facts, a found destination, a refusal's offered holder, the
    preview's destinations and the assignments it says also hold a card's words, the
    refusals, and each offer's moves and preview. Two of these that would read the same each
    show their reference, and an assignment a refusal names always shows it, wherever it is
    named on the page."""
    named: set[str] = set()
    always: set[str] = set()
    for unit in state.validation.units:
        for refusal in unit.refusals:
            if refusal.kind == "copy":
                always.update(refusal.named)
    for piece in state.open:
        here = {
            str(e.stands_for)
            for e in state.entries.get(piece.name, [])
            if e.kind not in ("find", "new_slot") and e.stands_for
        }
        chosen = state.mapping.get(piece.name)
        if chosen is not None and dest_kind(chosen) == "found":
            here.add(str(chosen["found"]))
        for refusal in state.validation.of(piece.name).refusals:
            if refusal.kind == "copy" and piece.name in refusal.pieces:
                here.add(refusal.named[1])
        for assignment_id in here & state.rows.keys():
            named.add(assignment_id)
            named.update(
                str(f.about)
                for f in facts(piece, state.rows[assignment_id], state.rows, state.picks)
                if f.about
            )
    previews = [state.preview]
    for offer, shown in reworked:
        previews.append(tuple(shown))
        for value in [v for _, v in offer.changes] + [v for _, v in offer.moved_from]:
            if value is not None:
                chosen = value_of(value)
                kind = dest_kind(chosen)
                if kind != "new":
                    named.add(str(chosen[kind]))
    for entries in previews:
        for entry in entries:
            if entry.stands_for is not None:
                named.add(entry.stands_for)
            named.update(entry.overlap)
    named |= always
    return labels_for([state.rows[i] for i in sorted(named) if i in state.rows], always)


def dates_said(piece: Piece, places: Mapping[str, int]) -> tuple[str, ...]:
    """What each of a piece's cards said of its dates, as read: its due line and the day
    heading above it, both kept whatever date Blossom uses."""

    def quoted(text: str | None) -> str:
        return SAID["nothing"] if not text else f"\u201c{text}\u201d"

    return tuple(
        f"Screenshot {places.get(one.place.sha256, 0)}, card {one.place.place + 1} said: "
        f"due line {quoted(one.line)}; day heading {quoted(one.heading)}."
        for one in piece.seen
    )


def due_option(piece: Piece, day: date) -> str:
    """One answer to which due date Blossom uses, by what said it."""
    lines = any(one.card == "due" and one.line_day == day for one in piece.seen)
    return f"Due {day_text(day)}, as the {'due line' if lines else 'day heading'} says"


def confirm_question(
    piece: Piece, field: str, values: Mapping[tuple[str, str], str], fold: str
) -> "Question":
    """Confirm as read, for a field the reader wasn't sure of, pointing to the box of "Correct
    what was read" that corrects it (``fold``, its fold's id), except for the due or assigned
    label, which is confirmed only."""
    subject = confirm_subject(piece.key, field)
    legend = SAID["unsure"].format(named=FIELD_NAMES[field])
    options = (("as_read", SAID["confirm"], values.get(("confirm", subject)) == "as_read"),)
    return Question(
        answer_field("confirm", subject),
        legend,
        options,
        fold="" if field == "badge" else fold,
    )


@dataclasses.dataclass(frozen=True)
class Box:
    """One box of "Correct what was read": its posted name, label, kind and whole current
    value, the digest of its value with no correction, and what is said beneath it."""

    name: str
    label: str
    kind: str
    """``text`` for one line, ``lines`` for several, ``date`` for a date."""
    value: str
    read: str
    refused: str | None = None
    mark: str | None = None
    hint: str | None = None
    none: tuple[str, bool] | None = None
    """For an assigned date: the name of "No assigned date was given", and whether it is
    checked."""
    carried: tuple[str, str] | None = None
    """For a typed date Blossom can't read: the hidden field that carries it back as typed, and
    its text, so a page posted unchanged keeps it."""

    @property
    def subject(self) -> str:
        """Its key and field, as its name and the signed list name it."""
        return self.name.removeprefix("correct:")


def box_name(key: str, field: str) -> str:
    """The posted name of a box."""
    return "correct:" + json.dumps([key, field], separators=(",", ":"))


def _text_box(
    key: str,
    field: str,
    label: str,
    kind: str,
    read: str,
    typed: str | None,
    unsaved: bool,
) -> Box:
    """A box of the row's text: its current value, the correction where there is one and the
    value as read where there isn't; with what a refusal says beneath it."""
    value = read if typed is None else typed
    named = BOX_NAMED["instruction" if field.startswith("instruction") else field]
    refused = None
    if typed == "" and field in NEEDED:
        refused = NEEDED[field]
    elif typed is not None and len(typed) > field_most(field):
        refused = SAID["too_long"].format(named=named, most=field_most(field))
    return Box(
        box_name(key, field),
        label,
        kind,
        value,
        read_digest(read),
        refused,
        SAID["not_saved_yet"] if unsaved else None,
    )


def _date_box(
    piece: Piece,
    field: str,
    typed: str | None,
    given: bool,
    shown: date | None,
    unsaved: bool,
    both: bool,
) -> Box:
    """A box of the piece's own date: a date field holding the current date, the correction
    where there is one; empty where none is current. A typed date Blossom can't read is said
    beneath it and carried back as typed beside it, never dropped."""
    key = dates_key(piece.name)
    name = box_name(key, field)
    read = piece.as_read
    as_read = None if read is None else (read.due if field == "due" else read.assigned)
    refused = carried = None
    value = "" if shown is None else shown.isoformat()
    if given and typed is not None:
        day = typed_day(typed) if typed else None
        if typed == "" and field in NEEDED:
            refused = NEEDED[field]
        elif len(typed) > field_most(field):
            refused = SAID["too_long"].format(named=BOX_NAMED[field], most=field_most(field))
            carried = (f"refused:{name.removeprefix('correct:')}", typed)
        elif typed and day is None:
            refused = SAID["unreadable"].format(value=typed)
            carried = (f"refused:{name.removeprefix('correct:')}", typed)
        value = "" if day is None else day.isoformat()
    if both:
        refused = SAID["both"]
    hint = None
    if field == "due" and not piece.due_resolved:
        hint = SAID["due_hint"]
    elif field == "assigned":
        hint = SAID["assigned_hint"]
    none = None
    if field == "assigned":
        none = ("none:" + name.removeprefix("correct:"), (given and typed is None) or both)
    return Box(
        name,
        BOX_LABELS[field],
        "date",
        value,
        read_digest("" if as_read is None else as_read.isoformat()),
        refused,
        SAID["not_saved_yet"] if unsaved else None,
        hint,
        none,
        carried,
    )


def fold_boxes(
    piece: Piece,
    row_boxes: bool,
    corrections: Corrections,
    unsaved: frozenset[tuple[str, str]],
    both: frozenset[str],
    texts: Sequence[str],
) -> tuple[Box, ...]:
    """The boxes of a card's "Correct what was read", each holding its field's whole current
    value: the row's class, title and one box per instruction text of ``texts`` (``box_texts``,
    one empty box where the reader wasn't sure of words it didn't read) when ``row_boxes`` (on
    the row's first open card), then the piece's due and assigned dates."""
    read = piece.as_read
    if read is None:
        return ()
    out: list[Box] = []
    row = corrections.get(piece.key, {})
    if row_boxes:
        for field in ("course", "title"):
            typed = row.get(field)
            out.append(
                _text_box(
                    piece.key,
                    field,
                    BOX_LABELS[field],
                    "text",
                    getattr(read, field),
                    typed,
                    (piece.key, field) in unsaved,
                )
            )
        old = row.get("instruction")
        for index, text in enumerate(texts):
            field = f"instruction:{index}"
            if field in row:
                typed = row[field]
                typed = "" if typed is None else typed
            elif isinstance(old, str) and usable_correction("instruction", old):
                typed = old.strip() if index == 0 else ""
            else:
                typed = None
            out.append(
                _text_box(
                    piece.key,
                    field,
                    BOX_LABELS["instruction"],
                    "lines",
                    text,
                    typed,
                    (piece.key, field) in unsaved,
                )
            )
    key = dates_key(piece.name)
    own = corrections.get(key, {})
    due = piece.due if piece.due_resolved else None
    out.append(
        _date_box(
            piece,
            "due",
            own.get("due"),
            "due" in own,
            due,
            (key, "due") in unsaved,
            False,
        )
    )
    out.append(
        _date_box(
            piece,
            "assigned",
            own.get("assigned"),
            "assigned" in own,
            piece.assigned,
            (key, "assigned") in unsaved,
            key in both,
        )
    )
    return tuple(out)


UNREAD_SAID: Final = {
    "text": (
        "Text Blossom couldn't read",
        "Look at the screenshot and enter it by hand if it's homework.",
    ),
    "no_day": (
        "Homework read, but its date couldn't be told",
        "No date was on the card or in a heading above it in this screenshot.",
    ),
    "clipped": (
        "Cut off at the screenshot's edge",
        "Often the same card shows whole in the next screenshot. If so, set this aside.",
    ),
    "repeated": (
        "Read twice the same way in one screenshot",
        "Set it aside if it's the same card.",
    ),
}
"""Each kind of card that couldn't be read: its group's heading and what it means."""
REASON_GROUP: Final = {
    "reading_unreadable": "text",
    "card_unreadable": "text",
    "incomplete": "text",
    "no_day": "no_day",
    "clipped": "clipped",
    "repeated": "repeated",
}


def unread_shown(
    unread: Sequence[UnreadCard],
    places: Mapping[str, int],
    open_keys: Sequence[str],
    page_key: bytes,
    version: int,
) -> tuple[tuple["UnreadGroup", ...], list["OutsideForm"]]:
    """The cards still open that couldn't be read, grouped by screenshot and reason, each with
    what was read of it and a "Set aside" form signed at the page's version."""
    still = set(open_keys)
    grouped: dict[tuple[int, str], list[tuple[str, str, str]]] = {}
    forms: list[OutsideForm] = []
    for index, item in enumerate(
        sorted(unread, key=lambda u: (places.get(u.sha256, 0), u.place or 0, u.key)), 1
    ):
        if item.key not in still:
            continue
        group = REASON_GROUP[item.reason]
        card = item.card
        parts = (
            []
            if card is None
            else [str(x) for x in (card.course, card.title, card.instruction, card.footer) if x]
        )
        read = " \u00b7 ".join(parts) if parts else "Nothing on it could be read."
        where = f"Card {item.place + 1}: " if item.place is not None else ""
        form_id = f"leave-item-{index}"
        keys = json.dumps([item.key], separators=(",", ":"))
        signed = [("version", str(version)), ("keys", keys)]
        signed.append(("check", page_check(page_key, signed)))
        forms.append(OutsideForm(form_id, "set-aside", tuple(signed)))
        shot = places.get(item.sha256, 0)
        about = f"card {item.place + 1}" if item.place is not None else read
        grouped.setdefault((shot, group), []).append(
            (where + read, form_id, f"{about} of screenshot {shot}")
        )
    out = []
    for (place, group), cards in sorted(grouped.items()):
        heading, said = UNREAD_SAID[group]
        out.append(
            UnreadGroup(f"Screenshot {place}: {heading}" if place else heading, said, tuple(cards))
        )
    return tuple(out), forms


def fact_text(fact_kind: str, entry_day: date | None, piece_day: date | None, about: str) -> str:
    """One fact beside an entry, as text."""
    if fact_kind == "due_differs":
        return f"Due {day_text(entry_day) or SAID['no_due']}: this card says {day_text(piece_day)}"
    if fact_kind == "assigned_differs":
        return f"Assigned {day_text(entry_day)}: this card says {day_text(piece_day)}"
    if fact_kind == "also_saved_in":
        return f"This card's words are also saved in {about}"
    return SAID[fact_kind]


# ------------------------------------------------------------------ what the page shows


@dataclasses.dataclass(frozen=True)
class Choice:
    """One radio of a piece's destination control."""

    value: str
    label: str
    facts: tuple[str, ...] = ()
    checked: bool = False


@dataclasses.dataclass(frozen=True)
class PieceControl:
    """One piece's destination control: its choices, the find button's form, and why it asks
    again, if it does."""

    name: str
    choices: tuple[Choice, ...]
    find: str
    """The id of the small form its "Find under another name…" button posts."""
    preselected: bool
    stale: bool
    lost: str | None = None
    """Why it asks again when its answer depended on corrections that changed since."""
    anchor: str = ""
    """Its fieldset's id on the page, by its place among the open pieces."""


@dataclasses.dataclass(frozen=True)
class Question:
    """A question one destination or one card asks, with nothing preselected that isn't
    stored."""

    field: str
    legend: str
    options: tuple[tuple[str, str, bool], ...]
    """(value, label, checked)."""
    many: bool = False
    helper: str | None = None
    leave: str | None = None
    """The "Leave this group unsaved" form a split question's row may be set aside with."""
    anchor: str = ""
    """Its fieldset's id on the page, by its place among the review's questions."""
    about: str = ""
    """The card or row it is about, which its legend names to a screen reader."""
    fold: str = ""
    """For a field the reader wasn't sure of: the id of the fold whose box corrects it."""
    notes: tuple[tuple[str, str], ...] = ()
    """Lines said above its options, each with the field of a question it links to, if any
    (resolved to that question's anchor once the page's questions are placed)."""
    answered: bool = False
    """For a field the reader wasn't sure of: whether a correction in the fold answers it, as
    it does for the save, with no option checked."""


@dataclasses.dataclass(frozen=True)
class OutsideForm:
    """A small form of its own, outside the review form, that a button there names: its id, its
    action under the review's address, and its signed fields."""

    id: str
    action: str
    fields: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class OfferShown:
    """An offer as its own form shows it: every card it moves, and what saving would do then."""

    moves: tuple[str, ...]
    lines: tuple[str, ...]
    form: str


@dataclasses.dataclass(frozen=True)
class CardHead:
    """What saving adds there as her week's card shows it: the class, the title, the due date,
    the school's words saving adds, and the quiet lines of what saving does."""

    course: str
    title: str
    due: str | None
    words: tuple[tuple[str, str], ...]
    """(how the words are led in, the school's words)."""
    quiet: tuple[str, ...]
    lines: tuple[str, ...] = ()
    """What saving does with the dates and instructions it was asked about, and where else
    the card's words are saved."""
    still_open: str | None = None


@dataclasses.dataclass(frozen=True)
class CardShown:
    """One card of the review: where it was read, its questions about what was read, its fold
    of corrections, what its screenshot said of its dates, where it goes, and its Type."""

    name: str
    about: str
    """The card by title and place, which its repeated controls' names end with."""
    sources: tuple[tuple[str, str, str], ...]
    """(where it was read, the screenshot's address, the screenshot's number)."""
    read_as: str | None
    checks: tuple[Question, ...]
    boxes: tuple[Box, ...]
    fold: str
    fold_open: bool
    said: tuple[str, ...]
    control: PieceControl
    type: Question | None = None


@dataclasses.dataclass(frozen=True)
class BlockShown:
    """One destination of a group, or a card with no destination yet: her week's card, the
    cards going there, and the destination's own questions."""

    head: CardHead
    cards: tuple[CardShown, ...]
    questions: tuple[Question, ...] = ()
    chosen: bool = True
    """Whether it is a destination, not a card still to place."""


@dataclasses.dataclass(frozen=True)
class UnitShown:
    """One group of what saving adds: its destinations and cards, whether it can be ticked to
    save, what it waits for, its refusals and their offers, and its "Leave this group unsaved"
    form."""

    unit: str
    blocks: tuple[BlockShown, ...]
    tick: bool
    ticked: bool
    """Ticked on the page, for display only: with saved progress, when its selection names every
    row of the group and the group is still open and can be ticked; with none, every group that
    can be ticked."""
    waits: tuple[str, ...]
    refusals: tuple[str, ...]
    offers: tuple[OfferShown, ...]
    leave: str
    anchor: str = ""
    """Its id on the page, by its place among the units."""
    about: str = ""
    """The unit's cards by title and place, which its repeated controls' names end with."""
    tick_label: str = ""


@dataclasses.dataclass(frozen=True)
class NameQuestion:
    """The student-name question for screenshots whose name isn't her confirmed one."""

    field: str
    said: str
    options: tuple[tuple[str, str, bool], ...]
    remove: tuple[tuple[str, str], ...]
    """Each image "Remove only this screenshot" names, with its button's label."""
    anchor: str = ""
    """Its fieldset's id on the page, by its place among the review's questions."""


@dataclasses.dataclass(frozen=True)
class RowsShown:
    """The review's rows as the page shows them."""

    names: tuple[NameQuestion, ...]
    splits: tuple[Question, ...]
    units: tuple[UnitShown, ...]
    hidden: tuple[tuple[str, str], ...]
    status: str
    forms: tuple[OutsideForm, ...]
    not_hers: bool = False
    """Whether "Not hers" is offered: with the name question, while nothing of the review is
    saved."""
    unread: tuple["UnreadGroup", ...] = ()
    """The cards that couldn't be read as homework, by screenshot and reason."""
    saved: tuple[str, ...] = ()
    """The cards of this review already saved, each by its title and place."""
    first_open: str | None = None
    """The id of the first fieldset, in page order, with no answer chosen or typed, or the fold
    whose box was refused."""
    words: Mapping[str, str] = dataclasses.field(default_factory=lambda: dict(SAID))
    """The fixed sentences and labels the page shows beside the rows' own."""


@dataclasses.dataclass(frozen=True)
class UnreadGroup:
    """Cards of one screenshot that couldn't be read for one reason: the group's heading, what
    it means, and each card with what was read of it and its "Set aside" form."""

    heading: str
    said: str
    cards: tuple[tuple[str, str, str], ...]
    """(the card's place and what was read of it, the id of its "Set aside" form, the card and
    screenshot its "Set aside" names)."""


@dataclasses.dataclass(frozen=True)
class ImageName:
    """What one read image's student line is: its status, its keyed form, and the name as read,
    which is shown on the page only and never logged."""

    image_id: str
    place: int
    sha256: str
    status: str
    form: str | None
    name: str | None


def page_check(page_key: bytes, fields: Sequence[tuple[str, str]]) -> str:
    """The signature of a form's signed fields under the review's page key."""
    body = json.dumps(sorted(fields), separators=(",", ":"))
    return hmac.new(page_key, body.encode(), hashlib.sha256).hexdigest()


def same_signature(given: str, expected: str) -> bool:
    """Whether a signature a form or a cookie sent is the one expected, compared as UTF-8 bytes
    in constant time, so a value with characters beyond ASCII is a mismatch, never an error."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def answer_field(kind: str, subject: str) -> str:
    """The form field an answer is posted in."""
    return f"answer:{kind}:{subject}"


# ------------------------------------------------------------------ the parts


def _choices(
    state: ReviewState, piece: Piece, defaulted: bool, labels: Mapping[str, str]
) -> tuple[Choice, ...]:
    """A piece's radios in its list's order, each assignment named as the whole page names
    it."""
    chosen = state.mapping.get(piece.name)
    picked = None if chosen is None else value_json(chosen)
    entries = state.entries.get(piece.name, [])
    out = []
    for entry in entries:
        if entry.kind == "find":
            continue
        if entry.kind == "new_slot":
            label = f"A new {piece.title} ({entry.slot})"
            out.append(Choice(entry.value, label, (), entry.value == picked))
            continue
        row = state.rows[str(entry.stands_for)]
        label = labels[row.assignment_id]
        if entry.kind == "linked":
            label = f"Saved before as {label}"
        elif entry.kind == "found_for_another":
            label = f"Found for another card here: {label}"
        if defaulted and entry.value == picked and entry.kind == "listed":
            label = f"Already saved in {label}."
        shown = tuple(
            fact_text(f.kind, f.entry_date, f.piece_date, labels.get(str(f.about), ""))
            for f in facts(piece, row, state.rows, state.picks)
        )
        out.append(Choice(entry.value, label, shown, entry.value == picked))
    for refusal in state.validation.of(piece.name).refusals:
        holder = refusal.named[1] if refusal.kind == "copy" else None
        offered = None if holder is None else value_json({"found": holder})
        if (
            holder is None
            or piece.name not in refusal.pieces
            or holder not in state.rows
            or holder in {e.stands_for for e in entries}
            or any(c.value == offered for c in out)
        ):
            continue
        shown = tuple(
            fact_text(f.kind, f.entry_date, f.piece_date, labels.get(str(f.about), ""))
            for f in facts(piece, state.rows[holder], state.rows, state.picks)
        )
        out.append(Choice(str(offered), labels[holder], shown, offered == picked))
    if picked is not None and all(c.value != picked for c in out) and chosen is not None:
        target = str(chosen.get("found", ""))
        if dest_kind(chosen) == "found" and target in state.rows:
            row = state.rows[target]
            shown = tuple(
                fact_text(f.kind, f.entry_date, f.piece_date, labels.get(str(f.about), ""))
                for f in facts(piece, row, state.rows, state.picks)
            )
            out.append(Choice(picked, f"Found under another name: {labels[target]}", shown, True))
    return tuple(out)


def _date_question(state: ReviewState, q: DateQuestion, dest_label: str) -> Question:
    word = "Due" if q.kind == "due" else "Assigned"
    stored = state.values.get((q.kind, q.subject))
    options = tuple(
        (
            day.isoformat(),
            f"{word} {day_text(day)}" + (", as saved" if source == "as_saved" else ""),
            stored == day.isoformat(),
        )
        for source, day in q.options
    )
    legend = f"{'Due date' if q.kind == 'due' else 'Assigned date'} for {dest_label}"
    return Question(answer_field(q.kind, q.subject), legend, options)


def _dest_label(
    state: ReviewState, entry: PreviewEntry, labels: Mapping[str, str], title: str
) -> str:
    if entry.stands_for is None:
        due = f"due {day_text(entry.due)}" if entry.due else SAID["no_due"]
        return f"A new {title}, {due}"
    label = labels[entry.stands_for]
    if entry.kind == "linked":
        return f"Saved before as {label}"
    if entry.kind == "found":
        return f"Found under another name: {label}"
    return label


def _kind_line(entry: PreviewEntry, row: SavedRow | None, lead: Piece) -> str | None:
    """The kind Save writes or keeps there, said only where it writes something: her choice,
    the title's suggestion on a new assignment, or the saved assignment's own kind; nothing
    while the kind question waits."""
    if entry.nothing_new or "kind" in entry.open_questions:
        return None
    if entry.written_kind in KINDS:
        return KIND_LINE["chosen"].format(kind=KIND_WORD[entry.written_kind])
    if row is None:
        if lead.reading is None:
            return None
        return KIND_LINE["suggested"].format(kind=KIND_WORD[lead.reading.kind.value])
    if row.kind not in KIND_WORD:
        return None
    return KIND_LINE["saved"].format(kind=KIND_WORD[row.kind])


def _assigned_line(entry: PreviewEntry, row: SavedRow | None, members: Sequence[Piece]) -> str:
    """The assigned date saving leaves there: a saved assignment's own date stays where the
    cards carry none, said as saved; none where none is written."""
    if entry.assigned is None:
        return SAID["no_assigned_line"]
    if (
        row is not None
        and row.assigned == entry.assigned
        and all(m.assigned is None for m in members)
    ):
        return f"Assigned {long_day(entry.assigned)}, as saved."
    return f"Assigned {long_day(entry.assigned)}."


def _effect_lines(
    state: ReviewState,
    entry: PreviewEntry,
    labels: Mapping[str, str],
    until: str,
    last_open: bool,
) -> list[str]:
    """What saving does with the dates a destination asked about, which instructions apply,
    and where else the cards' words are saved."""
    row = state.rows.get(entry.stands_for) if entry.stands_for else None
    members = [state.by_name[n] for n in entry.pieces]
    lines = []
    asked = {
        q.kind: q
        for q in state.validation.of(entry.pieces[0]).questions
        if q.subject == subject_for(entry.dest, entry.pieces)
    }
    if "due" in asked and entry.due is not None and "due" not in entry.open_questions:
        others = [d for _, d in asked["due"].options if d != entry.due]
        for other in others:
            lines.append(
                f"Due {day_text(entry.due)}, as you chose. The school's {day_text(other)} is "
                "kept beside it, and her page will say the sources disagree."
            )
    if "assigned" in asked and entry.assigned and "assigned" not in entry.open_questions:
        if row is not None and row.assigned is not None:
            lines.append(
                f"Assigned {day_text(row.assigned)} stays, as you chose."
                if entry.assigned == row.assigned
                else f"Its assigned date becomes {day_text(entry.assigned)}, as you chose. "
                f"The saved {day_text(row.assigned)} isn't kept."
            )
        for _, other in asked["assigned"].options:
            if other == entry.assigned or (row is not None and other == row.assigned):
                continue
            lines.append(
                f"{day_text(other)} isn't saved; it is removed from Blossom when this review "
                "closes, right after saving, or by the next hourly cleanup if that step fails."
                if last_open
                else f"{day_text(other)} isn't saved with the homework; it stays in this review "
                f"until {until}, and is removed from Blossom by the next hourly cleanup after that."
            )
    if entry.instruction_choice is not None:
        lines.append(
            "Instructions that apply: "
            + "; ".join(f"\u201c{t}\u201d" for t in entry.instruction_choice)
            + "."
            if entry.instruction_choice
            else "None of the instructions shown apply."
        )
    holders = [labels[i] for i in entry.overlap if i in labels]
    if holders and entry.stands_for is not None:
        lines += [f"Also saved in {h}." for h in holders]
    elif holders:
        whose = "These cards' words are" if len(members) > 1 else "This card's words are"
        lines.append(f"{whose} also saved in {' and '.join(holders)}.")
    return lines


def _card_head(
    state: ReviewState,
    entry: PreviewEntry,
    labels: Mapping[str, str],
    until: str,
    last_open: bool,
    still_open: str | None,
) -> CardHead:
    """What saving adds there, as her week's card: under the saved assignment's class and
    title, or the lead card's for a new one, its due date, the school's words saving adds,
    then the assigned date, what saving does and the kind it writes or keeps."""
    row = state.rows.get(entry.stands_for) if entry.stands_for else None
    members = sorted((state.by_name[n] for n in entry.pieces), key=lambda p: p.name)
    lead = members[0]
    course, title = (row.course, row.title) if row is not None else (lead.course, lead.title)
    due = f"Due {long_day(entry.due)}" if entry.due else SAID["no_due_line"]
    words: tuple[tuple[str, str], ...] = ()
    if entry.nothing_new:
        effect = SAID["already"]
    else:
        effect = SAID["new_assignment"] if row is None else SAID["updates"].format(title=row.title)
        lead_in = "From the school:" if row is None else "Adds"
        words = tuple((lead_in, text) for text in entry.adds)
    kind = _kind_line(entry, row, lead)
    quiet = [_assigned_line(entry, row, members), effect if kind is None else f"{effect} {kind}"]
    if not entry.nothing_new and not entry.adds:
        quiet.insert(0, SAID["no_new_words"])
    return CardHead(
        course,
        title,
        due,
        words,
        tuple(quiet),
        tuple(_effect_lines(state, entry, labels, until, last_open)),
        still_open,
    )


def _own_head(piece: Piece) -> CardHead:
    """A card with no destination yet, as it was read and corrected: its class, title, due
    date when settled, its words and its assigned date."""
    due = f"Due {long_day(piece.due)}" if piece.due and piece.due_resolved else None
    texts = row_texts_of(piece)
    assigned = (
        SAID["no_assigned_line"]
        if piece.assigned is None
        else f"Assigned {long_day(piece.assigned)}."
    )
    return CardHead(
        piece.course,
        piece.title,
        due,
        tuple(("From the school:", text) for text in texts),
        (assigned,),
    )


def row_texts_of(piece: Piece) -> tuple[str, ...]:
    """A piece's instruction texts as corrected, in reading order."""
    if piece.reading is None:
        return tuple(sorted(piece.words))
    return tuple(dict.fromkeys(i.text for i in piece.reading.instructions))


def _preview_text(state: ReviewState, entry: PreviewEntry, labels: Mapping[str, str]) -> list[str]:
    """What saving does there, as an offer's lines say it in one paragraph."""
    head = _card_head(state, entry, labels, "", False, None)
    lines = [] if head.due is None else [f"{head.due}."]
    lines += [f"{lead} \u201c{text}\u201d" for lead, text in head.words]
    return [*lines, *head.quiet, *head.lines]


def _refusal_text(
    refusal: Refusal, state: ReviewState, places: Mapping[str, int], labels: Mapping[str, str]
) -> str:
    cards = " and ".join(piece_text(places, state.by_name[n]) for n in refusal.pieces)
    if refusal.kind == "split_siblings":
        return f"{cards} are different days of one row, so they can't go to the same assignment."
    dest, holder = refusal.named
    return (
        f"{cards} would copy the words of {labels.get(holder, holder)} into "
        f"{labels.get(dest, dest)}, so this group isn't saved."
    )


def _offer_shown(
    offer: Offer,
    shown: Sequence[PreviewEntry],
    state: ReviewState,
    places: Mapping[str, int],
    labels: Mapping[str, str],
    page_key: bytes,
    version: int,
    form_id: str,
) -> tuple[OfferShown, OutsideForm]:
    def where(value: str | None, piece: Piece) -> str:
        if value is None:
            return "no choice yet"
        chosen = value_of(value)
        kind = dest_kind(chosen)
        if kind == "new":
            return f"a new {piece.title} ({chosen['new']})"
        return labels.get(str(chosen[kind]), str(chosen[kind]))

    before = dict(offer.moved_from)
    moves = tuple(
        f"{piece_text(places, state.by_name[n])}: from {where(before.get(n), state.by_name[n])}"
        f" to {where(v, state.by_name[n])}"
        for n, v in offer.changes
    )
    moved = {n for n, _ in offer.changes}
    lines = []
    for entry in shown:
        if not moved & set(entry.pieces):
            continue
        title = state.by_name[entry.pieces[0]].title
        label = _dest_label(state, entry, labels, title)
        lines.append(f"{label}: " + " ".join(_preview_text(state, entry, labels)))
    fields = [("version", str(version)), ("press", "keep")]
    fields += [(answer_field("destination", n), v) for n, v in offer.changes]
    fields.append(("offer", page_check(page_key, fields)))
    form = OutsideForm(f"offer-{form_id}", "review", tuple(fields))
    return OfferShown(moves, tuple(lines), form.id), form


def signed_action(action: str, page_key: bytes, version: int) -> OutsideForm:
    """A small signed form for a press on the whole review, "Discard the review" or "Not
    hers", at the page's version: its id is its action."""
    signed = [("version", str(version))]
    signed.append(("check", page_check(page_key, [*signed, ("action", action)])))
    return OutsideForm(action, action, tuple(signed))


def _names(
    images: Sequence[ImageName], values: Mapping[tuple[str, str], str], frozen: bool = False
) -> tuple[list[NameQuestion], set[str]]:
    """The name questions, and the images whose rows wait on one. Once some homework is saved,
    no screenshot can be removed, so none offers "Remove only this screenshot"."""
    out: list[NameQuestion] = []
    waiting: set[str] = set()
    missing = [i for i in images if i.status == "missing"]
    if missing:
        got = values.get(("name", "missing"))
        out.append(
            NameQuestion(
                answer_field("name", "missing"),
                f"No student name was found on {len(missing)} screenshot"
                f"{'' if len(missing) == 1 else 's'}. Confirm these are her homework before "
                "saving.",
                (("confirmed", "These are her homework", got == "confirmed"),),
                (),
            )
        )
        if got != "confirmed":
            waiting |= {i.sha256 for i in missing}
    by_form: dict[str, list[ImageName]] = {}
    for image in images:
        if image.status not in ("matches", "missing") and image.form is not None:
            by_form.setdefault(image.form, []).append(image)
    for form, shown in by_form.items():
        got = values.get(("name", form))
        first = shown[0]
        said = (
            f"Screenshot {first.place} names {first.name}. Is this her name? Answer before "
            "saving its rows."
            if first.status == "first_use"
            else f"Screenshot {first.place} names {first.name}, which isn't her confirmed name. "
            "Answer before saving its rows."
        )
        out.append(
            NameQuestion(
                answer_field("name", form),
                said,
                (
                    ("hers", "Yes, this is her name", got == "hers"),
                    ("misread", "The name was misread", got == "misread"),
                ),
                ()
                if frozen
                else tuple(
                    (
                        i.image_id,
                        "Remove only this screenshot"
                        if len(shown) == 1
                        else f"Remove only screenshot {i.place}",
                    )
                    for i in shown
                ),
            )
        )
        if got not in ("hers", "misread"):
            waiting |= {i.sha256 for i in shown}
    return out, waiting


def _split_question(
    row: ScreenshotRow, places: Mapping[str, int], values: Mapping[tuple[str, str], str]
) -> Question:
    stored = values.get(("split", row.key))
    options = [
        (
            json.dumps(["one", day.day.isoformat()]),
            f"Assigned {day_text(day.day)} ("
            + "; ".join(card_text(places, c) for c in day.sources)
            + ")",
            stored == json.dumps(["one", day.day.isoformat()]),
        )
        for day in row.assigned
    ]
    options.append((json.dumps("split"), SAID["split_apart"], stored == json.dumps("split")))
    legend = f"{row.reading.title}: {SAID['split']}"
    cards = "; ".join(card_text(places, c) for day in row.assigned for c in day.sources)
    return Question(
        answer_field("split", row.key),
        legend,
        tuple(options),
        about=f"{row.reading.title} ({cards})",
    )


def rows_shown(
    state: ReviewState,
    *,
    places: Mapping[str, int],
    images: Sequence[ImageName],
    page_key: bytes,
    version: int,
    progress_revision: int,
    acceptance: str,
    until: str,
    corrections: Corrections,
    selected: frozenset[str] | None = None,
    open_keys: Sequence[str] = (),
    unread: Sequence[UnreadCard] = (),
    unsaved: frozenset[tuple[str, str]] = frozenset(),
    frozen: bool = False,
    image_ids: Mapping[str, str] | None = None,
    href: str = "",
    both: frozenset[str] = frozenset(),
) -> RowsShown:
    """The review's rows: the name questions, the split questions, and one group of what
    saving adds per unit, each destination as her week's card with the cards going there and
    its own questions, each card with its source, its questions about what was read, its fold
    of corrections and where it goes, a row's Type on its first open card; then the group's
    refusals, offers and tick, plus the signed fields the review form carries."""
    values = dict(state.values)
    image_ids = image_ids or {}
    reworked: dict[tuple[str, ...], list[tuple[Offer, Sequence[PreviewEntry]]]] = {}
    for unit in state.validation.units:
        if not unit.refusals:
            continue
        entry_values = {
            n: [e.value for e in state.entries.get(n, []) if e.kind != "find"] for n in unit.unit
        }

        def recheck(mapping: Mapping[str, Destination]) -> Validation:
            return state.rework(mapping)[0]

        reworked[unit.unit] = [
            (
                offer,
                state.rework({**state.mapping, **{n: value_of(v) for n, v in offer.changes}})[1],
            )
            for offer in offers(state.pieces, state.mapping, unit, entry_values, recheck)
        ]
    labels = page_labels(state, [pair for found in reworked.values() for pair in found])
    names, waiting = _names(images, values, frozen)
    splits = tuple(_split_question(r, places, values) for r in state.asking)

    def first_card(piece: Piece) -> tuple[int, int, str]:
        cards = [(places.get(c.sha256, 0), c.place) for c in piece.cards] or [(0, 0)]
        return (*min(cards), piece.name)

    open_pieces = sorted(state.open, key=first_card)
    order = {piece.name: index for index, piece in enumerate(open_pieces)}
    finds = {piece.name: f"find-{number}" for number, piece in enumerate(open_pieces, 1)}
    choices_at = {piece.name: f"card-{number}" for number, piece in enumerate(open_pieces, 1)}
    folds = {piece.name: f"fix-{number}" for number, piece in enumerate(open_pieces, 1)}
    first_of_row: dict[str, str] = {}
    for piece in open_pieces:
        first_of_row.setdefault(piece.key, piece.name)
    confirm_on: dict[tuple[str, str], str] = {}
    for piece in open_pieces:
        for field in sorted(confirm_fields(piece)):
            host = piece.name if field in ("footer", "heading") else first_of_row[piece.key]
            confirm_on.setdefault((piece.key, field), host)
    previews = {e.dest: e for e in state.preview}
    digests = unit_digests(state)
    wordless = wordless_rows(open_pieces)

    def card_of(piece: Piece, head: CardHead | None) -> CardShown:
        about = piece_text(places, piece)
        own = first_of_row[piece.key] == piece.name
        checks: list[Question] = []
        if piece.due_choices and not settles_due(piece, corrections):
            got = values.get(("due_read", piece.name))
            checks.append(
                Question(
                    answer_field("due_read", piece.name),
                    SAID["due_differs"],
                    tuple(
                        (day.isoformat(), due_option(piece, day), got == day.isoformat())
                        for day in piece.due_choices
                    ),
                    about=about,
                )
            )
        for (key, field), host in confirm_on.items():
            if host != piece.name:
                continue
            fold = folds[first_of_row[key]] if field not in ("footer", "heading") else folds[host]
            checks.append(
                dataclasses.replace(
                    confirm_question(piece, field, values, fold),
                    about=about,
                    answered=fold_answers(piece, field, corrections),
                )
            )
        boxes = fold_boxes(piece, own, corrections, unsaved, both, box_texts(piece, wordless))
        sources = tuple(
            (
                SAID["source"].format(shot=places.get(c.sha256, 0), card=c.place + 1),
                f"{href}/images/{image_ids[c.sha256]}" if c.sha256 in image_ids else "",
                str(places.get(c.sha256, 0)),
            )
            for c in piece.cards
        )
        read_as = None
        course, title = (
            (piece.course, piece.title)
            if piece.as_read is None
            else (piece.as_read.course, piece.as_read.title)
        )
        if head is not None and (folded(head.course), folded(head.title)) != (
            folded(course),
            folded(title),
        ):
            named = title if folded(course) == folded(head.course) else f"{course}: {title}"
            read_as = SAID["read_as"].format(title=named)
        control = PieceControl(
            piece.name,
            _choices(state, piece, piece.name in state.defaulted, labels),
            finds[piece.name],
            piece.name in state.defaults and piece.name not in state.stored,
            piece.name in state.validation.of(piece.name).stale,
            SAID["lost_correction"] if piece.name in state.lost else None,
            choices_at[piece.name],
        )
        kind = None
        if own:
            got = values.get(("kind", piece.key))
            row_pieces = [p for p in open_pieces if p.key == piece.key]
            kind = Question(
                answer_field("kind", piece.key),
                SAID["type"],
                (
                    ("HOMEWORK", SAID["homework"], got == "HOMEWORK"),
                    ("TASK", SAID["task"], got == "TASK"),
                ),
                helper=SAID["type_helper"],
                about=about,
                notes=_type_lines(state, row_pieces, labels, places, previews),
            )
        return CardShown(
            piece.name,
            about,
            sources,
            read_as,
            tuple(checks),
            boxes,
            folds[piece.name],
            any(box.refused for box in boxes),
            dates_said(piece, places),
            control,
            kind,
        )

    ordered = sorted(
        state.validation.units, key=lambda u: min(first_card(state.by_name[n]) for n in u.unit)
    )
    forms = [OutsideForm(form_id, "find", (("piece", name),)) for name, form_id in finds.items()]
    shown_units = []
    for number, unit in enumerate(ordered, 1):
        last_open = set(unit.unit) >= {p.name for p in open_pieces}
        blocks: list[tuple[int, BlockShown]] = []
        placed: set[str] = set()
        still_any = False
        for dest, names_there in state.validation.destinations.items():
            if not set(names_there) <= set(unit.unit) or dest not in previews:
                continue
            entry = previews[dest]
            members = sorted((state.by_name[n] for n in entry.pieces), key=first_card)
            still = _still_open(entry, members, values, waiting, corrections)
            still_any = still_any or bool(still)
            said_open = (
                None
                if not still
                else "Still to answer: " + ", ".join(OPEN_NAMES[q] for q in still) + "."
            )
            head = _card_head(state, entry, labels, until, last_open, said_open)
            label = _dest_label(state, entry, labels, members[0].title)
            blocks.append(
                (
                    min(order[m.name] for m in members),
                    BlockShown(
                        head,
                        tuple(card_of(m, head) for m in members),
                        tuple(_dest_questions(state, entry, label, values)),
                    ),
                )
            )
            placed |= set(entry.pieces)
        for name in unit.unit:
            if name in placed:
                continue
            piece = state.by_name[name]
            blocks.append(
                (order[name], BlockShown(_own_head(piece), (card_of(piece, None),), (), False))
            )
        blocks.sort(key=lambda block: block[0])
        refusals = tuple(_refusal_text(r, state, places, labels) for r in unit.refusals)
        shown_offers: list[OfferShown] = []
        for index, (offer, reshown) in enumerate(reworked.get(unit.unit, []), 1):
            shown, form = _offer_shown(
                offer, reshown, state, places, labels, page_key, version, f"{number}-{index}"
            )
            shown_offers.append(shown)
            forms.append(form)
        keys = json.dumps(sorted({state.by_name[n].key for n in unit.unit}), separators=(",", ":"))
        leave = [("version", str(version)), ("keys", keys)]
        leave.append(("check", page_check(page_key, leave)))
        forms.append(OutsideForm(f"leave-{number}", "set-aside", tuple(leave)))
        tickable = unit.ready and not still_any and unit_name(unit.unit) in digests
        held = sorted((state.by_name[n] for n in unit.unit), key=first_card)
        chosen_blocks = sum(1 for _, block in blocks if block.chosen)
        shown_units.append(
            UnitShown(
                unit_name(unit.unit),
                tuple(block for _, block in blocks),
                tickable,
                tickable
                and (
                    selected is None or {state.by_name[name].key for name in unit.unit} <= selected
                ),
                _waits(unit),
                refusals,
                tuple(shown_offers),
                f"leave-{number}",
                f"saving-{number}",
                "; ".join(piece_text(places, m) for m in held),
                SAID["tick_one"] if chosen_blocks <= 1 else SAID["tick_many"],
            )
        )
    shown_units = _placed(names, splits, shown_units)
    names = [dataclasses.replace(q, anchor=f"ask-{n}") for n, q in enumerate(names, 1)]
    splits = tuple(
        dataclasses.replace(q, anchor=f"ask-{n}") for n, q in enumerate(splits, len(names) + 1)
    )
    asked_fields: list[tuple[str, str]] = [
        (q.field, json.dumps([o[0] for o in q.options], separators=(",", ":"))) for q in names
    ] + [(q.field, json.dumps([o[0] for o in q.options], separators=(",", ":"))) for q in splits]
    for shown_unit in shown_units:
        for block in shown_unit.blocks:
            for card in block.cards:
                asked_fields.append(
                    (
                        answer_field("destination", card.name),
                        json.dumps([c.value for c in card.control.choices], separators=(",", ":")),
                    )
                )
                for question in (*card.checks, *((card.type,) if card.type else ())):
                    offered = [o[0] for o in question.options]
                    asked_fields.append(
                        (question.field, json.dumps(offered, separators=(",", ":")))
                    )
                for box in card.boxes:
                    asked_fields.append((box.name, "[]"))
                    if box.none is not None:
                        asked_fields.append((box.none[0], json.dumps(["none"])))
            for question in block.questions:
                offered = [o[0] for o in question.options]
                asked_fields.append((question.field, json.dumps(offered, separators=(",", ":"))))
    hidden = [
        ("version", str(version)),
        ("progress_revision", str(progress_revision)),
        ("acceptance", acceptance),
        ("asked", json.dumps(sorted(asked_fields), separators=(",", ":"))),
        (
            "defaults",
            json.dumps(
                {n: value_json(v) for n, v in sorted(state.defaults.items())},
                separators=(",", ":"),
            ),
        ),
        ("identity", json.dumps(sorted({i.status for i in images}), separators=(",", ":"))),
    ]
    hidden += [(f"digest:{u}", ",".join(d)) for u, d in sorted(digests.items())]
    hidden.append(("check", page_check(page_key, hidden)))
    unread_groups, unread_forms = unread_shown(unread, places, open_keys, page_key, version)
    for index, row in enumerate(state.asking, 1):
        keys = json.dumps([row.key], separators=(",", ":"))
        signed = [("version", str(version)), ("keys", keys)]
        signed.append(("check", page_check(page_key, signed)))
        forms.append(OutsideForm(f"leave-split-{index}", "set-aside", tuple(signed)))
    splits = tuple(
        dataclasses.replace(question, leave=f"leave-split-{index}")
        for index, question in enumerate(splits, 1)
    )
    finishing = [
        ("version", str(version)),
        ("keys", json.dumps(sorted(open_keys), separators=(",", ":"))),
    ]
    finishing.append(("check", page_check(page_key, [*finishing, ("action", "finish")])))
    forms.append(OutsideForm("finish", "finish", tuple(finishing)))
    forms.append(signed_action("not-hers", page_key, version))
    return RowsShown(
        tuple(names),
        splits,
        tuple(shown_units),
        tuple(hidden),
        SAID["status"],
        (*forms, *unread_forms),
        bool(names) and not state.saved,
        unread_groups,
        tuple(piece_text(places, p) for p in state.pieces if p.name in state.saved),
        _first_open(names, splits, shown_units),
    )


def _dest_questions(
    state: ReviewState, entry: PreviewEntry, label: str, values: Mapping[tuple[str, str], str]
) -> list[Question]:
    """A destination's own questions: its due and assigned dates, its kind where its cards'
    choices disagree or differ from the saved one, and which instructions apply."""
    out: list[Question] = []
    unit = state.validation.of(entry.pieces[0])
    for q in unit.questions:
        if q.subject == subject_for(entry.dest, entry.pieces):
            out.append(_date_question(state, q, label))
    for kq in unit.kind_questions:
        if kq.subject == subject_for(entry.dest, entry.pieces):
            got = values.get(("kind", kq.subject))
            out.append(
                Question(
                    answer_field("kind", kq.subject),
                    f"Homework or task for {label}?",
                    (
                        ("HOMEWORK", SAID["homework"], got == "HOMEWORK"),
                        ("TASK", SAID["task"], got == "TASK"),
                    ),
                )
            )
    change = state.asked.get(entry.dest)
    if change is not None:
        subject = instruction_subject(entry.dest, list(entry.pieces), change.instructions_revision)
        got = values.get(("instructions", subject))
        chosen = set(json.loads(got)) if got else set()
        # Each instruction travels as one JSON string in ASCII, so a browser posts it back
        # exactly, line endings included; "none apply" is the empty value.
        options = tuple(
            (to_wire(s.text), f"\u201c{s.text}\u201d", s.text in chosen)
            for s in change.instructions_shown
        )
        out.append(
            Question(
                answer_field("instructions", subject),
                f"Which instructions apply to {label}?",
                (*options, ("", SAID["none_apply"], got == "[]")),
                many=True,
            )
        )
    return out


def _type_lines(
    state: ReviewState,
    pieces: Sequence[Piece],
    labels: Mapping[str, str],
    places: Mapping[str, int],
    previews: Mapping[tuple[str, ...], PreviewEntry],
) -> tuple[tuple[str, str], ...]:
    """What saving does with a row's Type, by Save's own rule, one line per open piece of the
    row, each led by its date when the row has more than one."""
    out = []
    for piece in pieces:
        text, target = _type_line(state, piece, labels, places, previews)
        if len(pieces) > 1:
            text = f"{day_text(piece.assigned or piece.due)}: {text}"
        out.append((text, target))
    return tuple(out)


def _type_line(
    state: ReviewState,
    piece: Piece,
    labels: Mapping[str, str],
    places: Mapping[str, int],
    previews: Mapping[tuple[str, ...], PreviewEntry],
) -> tuple[str, str]:
    """The kind Save writes for a piece's destination and where it comes from, as
    ``destination_kind`` and the save work it out: a destination's kind question answered or
    waiting, her choice on this row or on another card going there, else the lead card's
    title's suggestion on a new assignment or the saved assignment's kind. A piece with no
    destination waits for one. With the field of the question it links to, if any."""
    value = state.mapping.get(piece.name)
    dest = None if value is None else dest_key(piece, value)
    names = None if dest is None else state.validation.destinations.get(dest)
    if dest is None or not names or piece.name not in names:
        return SAID["choose_first"], ""
    members = [state.by_name[n] for n in names]
    row = state.rows.get(dest[1]) if dest[0] != "new" else None
    question = kind_question(dest, members, row, state.kinds)
    if question is not None:
        got = state.values.get(("kind", question.subject))
        if got not in KINDS:
            entry = previews[dest]
            label = _dest_label(state, entry, labels, members[0].title)
            return SAID["kind_first"].format(label=label), answer_field("kind", question.subject)
        return TYPE_LINE["chosen"].format(kind=KIND_WORD[got]), ""
    if piece.name in state.kinds:
        return TYPE_LINE["chosen"].format(kind=KIND_WORD[state.kinds[piece.name]]), ""
    chosen = sorted(
        (m for m in members if m.name in state.kinds),
        key=lambda m: (min((places.get(c.sha256, 0), c.place) for c in m.cards), m.name),
    )
    if chosen:
        other = chosen[0]
        kind = KIND_WORD[state.kinds[other.name]]
        return TYPE_LINE["chosen_for"].format(kind=kind, card=piece_text(places, other)), ""
    if row is None:
        lead = sorted(members, key=lambda m: m.name)[0]
        if lead.reading is None:
            return SAID["choose_first"], ""
        return TYPE_LINE["suggested"].format(kind=KIND_WORD[lead.reading.kind.value]), ""
    return TYPE_LINE["saved"].format(kind=KIND_WORD.get(row.kind or "", "homework")), ""


def _placed(
    names: Sequence[NameQuestion], splits: Sequence[Question], units: Sequence[UnitShown]
) -> list[UnitShown]:
    """Every card's and destination's questions with their anchors, numbered after the name
    and split questions in the order the page shows them, and each line under a Type that
    links to a question pointing at that question's anchor."""
    number = len(names) + len(splits)
    anchors: dict[str, str] = {}

    def anchored(question: Question) -> Question:
        nonlocal number
        number += 1
        anchors[question.field] = f"ask-{number}"
        return dataclasses.replace(question, anchor=f"ask-{number}")

    out = []
    for unit in units:
        blocks = []
        for block in unit.blocks:
            cards = []
            for card in block.cards:
                checks = tuple(anchored(q) for q in card.checks)
                kind = None if card.type is None else anchored(card.type)
                cards.append(dataclasses.replace(card, checks=checks, type=kind))
            questions = tuple(anchored(q) for q in block.questions)
            blocks.append(dataclasses.replace(block, cards=tuple(cards), questions=questions))
        out.append(dataclasses.replace(unit, blocks=tuple(blocks)))

    def linked(question: Question | None) -> Question | None:
        if question is None or not question.notes:
            return question
        notes = tuple((text, anchors.get(field, "")) for text, field in question.notes)
        return dataclasses.replace(question, notes=notes)

    return [
        dataclasses.replace(
            unit,
            blocks=tuple(
                dataclasses.replace(
                    block,
                    cards=tuple(
                        dataclasses.replace(card, type=linked(card.type)) for card in block.cards
                    ),
                )
                for block in unit.blocks
            ),
        )
        for unit in out
    ]


def _first_open(
    names: Sequence[NameQuestion], splits: Sequence[Question], units: Sequence[UnitShown]
) -> str | None:
    """The id of the first place still to answer: a fold whose box a press refused, since
    nothing of that press was kept until it is put right; else, in the order the page shows
    them, a fieldset with no option checked, an unsure field answered by its fold's correction
    aside. A Type left unchosen saves as its line says, so it is never still to answer."""

    def answered(question: Question | NameQuestion) -> bool:
        corrected = isinstance(question, Question) and question.answered
        return corrected or any(checked for _, _, checked in question.options)

    cards = [card for unit in units for block in unit.blocks for card in block.cards]
    refused = next((card.fold for card in cards if card.fold_open), None)
    if refused is not None:
        return refused
    asked = [(q.anchor, answered(q)) for q in names]
    asked += [(q.anchor, answered(q)) for q in splits]
    for unit in units:
        for block in unit.blocks:
            for card in block.cards:
                asked += [(q.anchor, answered(q)) for q in card.checks]
                asked.append((card.control.anchor, any(c.checked for c in card.control.choices)))
            asked += [(q.anchor, answered(q)) for q in block.questions]
    return next((anchor for anchor, done in asked if not done), None)


def _still_open(
    entry: PreviewEntry,
    members: Sequence[Piece],
    values: Mapping[tuple[str, str], str],
    waiting: set[str],
    corrections: Corrections,
) -> list[str]:
    """A destination's questions still open: its dates and kind, its instructions, a field the
    reader wasn't sure of, a date typed as text Blossom can't read, which due date Blossom
    uses, and the student name of a screenshot its cards came from."""
    still = list(entry.open_questions)
    if entry.instruction_revision is not None and entry.instruction_choice is None:
        still.append("instructions")
    if any(
        values.get(("confirm", confirm_subject(m.key, f))) is None
        and not fold_answers(m, f, corrections)
        for m in members
        for f in confirm_fields(m)
    ):
        still.append("confirm")
    for field in sorted({f for m in members for f in unreadable_dates(m, corrections)}):
        if field not in still:
            still.append(field)
    if any(not m.due_resolved for m in members):
        still.append("due_read")
    if any(c.sha256 in waiting for m in members for c in m.cards):
        still.append("name")
    return still


def _waits(unit: UnitCheck) -> tuple[str, ...]:
    out = []
    if unit.unanswered:
        out.append(SAID["choose_each"])
    if unit.stale:
        out.append(SAID["choose_again"])
    if unit.refusals:
        out.append(SAID["stays_open"])
    return tuple(out)
