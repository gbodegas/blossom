# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The screenshot review as a parent reads it: one destination control per piece, the questions
each destination asks, and the group preview with its refusals and offers.

Everything here is worked out from the review's state, which the save works from too, so the
page and the save agree. A preselected destination is shown checked and is never stored until
a press posts it. Every sentence the review's rows can show is in this module, in one place.
"""

import dataclasses
import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Final

from blossom.screenshot_review import (
    ChangeTold,
    DateQuestion,
    Destination,
    Offer,
    Piece,
    PreviewEntry,
    Refusal,
    SavedRow,
    UnitCheck,
    Validation,
    dest_kind,
    facts,
    offers,
    reference,
    subject_for,
    typed_day,
    unit_name,
    value_json,
    value_of,
)
from blossom.screenshot_save import (
    CORRECTABLE,
    DATE_FIELDS,
    ReviewState,
    confirm_fields,
    confirm_subject,
    instruction_subject,
    unit_digests,
)
from blossom.screenshots import CardPlace, ScreenshotRow, UnreadCard

SAID: Final = {
    "status": (
        "Answers are kept when you submit them. Corrections you type are kept only when you "
        "press Save review for later."
    ),
    "preselected": "Preselected; kept when you press a button below.",
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
    "type": "Type",
    "type_helper": "Task: a form to sign, supplies to bring, or similar.",
    "due_differs": "Its due line and its day heading name different dates. Which due date does "
    "Blossom use?",
    "correction": "Correction",
    "nothing": "none",
    "lost_correction": "This row's answer depended on a correction that wasn't kept, so it asks "
    "again.",
}
"""The fixed sentences and labels of the review's rows."""

CHANGE_SAID: Final = {
    "cleared": "Your screenshots changed, so these rows' answers were cleared: {labels}. Answer "
    "them again.",
    "copies": "Blossom can't tell which of these identical cards is which after the change, so "
    "their answers were cleared.",
    "carried": "Your screenshots changed. {label} is still left unsaved, as you chose for these "
    "cards.",
    "asked": "Your screenshots changed. {label} now has cards you hadn't left unsaved, so it asks "
    "again. You can leave it unsaved again.",
}
"""What the page after a change of screenshots says of the answers it cleared and the rows left
unsaved it kept or asks about again."""

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

WRITTEN_KIND: Final = {"HOMEWORK": "Saved as homework.", "TASK": "Saved as a task."}


def day_text(day: date | None) -> str:
    """A day as the review's rows say it: "Wed 10/14"."""
    return "" if day is None else f"{day:%a} {day.month}/{day.day}"


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
    can't be told apart, when any of those rows is one of them; and each row left unsaved it
    kept or asks about again."""
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
    piece: Piece,
    field: str,
    places: Mapping[str, int],
    values: Mapping[tuple[str, str], str],
    typed: Mapping[str, str],
    unsaved: bool,
) -> "Question":
    """Confirm as read, and for a field a parent may correct, its box: a date for the due line
    or the day heading, text otherwise. The due or assigned label is confirmed only. A typed
    date Blossom can't read is said beneath its box, never dropped."""
    subject = confirm_subject(piece.key, field)
    named = FIELD_NAMES[field]
    if field == "footer" or (field == "heading" and piece.due_card):
        named = "due date"
    legend = f"{piece_text(places, piece)}: The reader wasn't sure of the {named}."
    options = (("as_read", SAID["confirm"], values.get(("confirm", subject)) == "as_read"),)
    if field not in CORRECTABLE:
        return Question(answer_field("confirm", subject), legend, options)
    value = typed.get(field, "")
    refused = None
    label, kind = SAID["correction"], "text"
    if field in DATE_FIELDS:
        kind = "date"
        label = "Due date" if named == "due date" else "Assigned date (day heading)"
        day = typed_day(value) if value else None
        if value and day is None:
            refused = (
                f"The date given was \u201c{value}\u201d, which is not a date Blossom can read."
            )
        value = "" if day is None else day.isoformat()
    return Question(
        answer_field("confirm", subject),
        legend,
        options,
        text=(f"correct:{subject}", value),
        unsaved=unsaved,
        text_label=label,
        text_type=kind,
        refused=refused,
        mark=SAID["not_saved_yet"] if unsaved else None,
    )


def piece_read(piece: Piece, course: str) -> str:
    """What was read for a card, as corrected, in the form an entry takes: its title (with its
    class where the group's heading gives another), due date, assigned date and words."""
    title = piece.title if piece.course == course else f"{piece.course}: {piece.title}"
    due = f"due {day_text(piece.due)}" if piece.due else SAID["no_due"]
    assigned = f"assigned {day_text(piece.assigned)}" if piece.assigned else SAID["no_assigned"]
    texts = list(dict.fromkeys(i.text for i in piece.reading.instructions)) if piece.reading else []
    words = ", ".join(f"\u201c{text}\u201d" for text in texts) or SAID["no_words"]
    return f"This card: {title}, {due}, {assigned}, {words}"


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
    grouped: dict[tuple[int, str], list[tuple[str, str]]] = {}
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
        grouped.setdefault((places.get(item.sha256, 0), group), []).append((where + read, form_id))
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
    """One piece's destination control: its legend, its choices and the find link."""

    name: str
    legend: str
    choices: tuple[Choice, ...]
    find: str
    preselected: bool
    stale: bool
    read: str = ""
    """What was read for the card, as corrected: its title, due date, assigned date and words,
    in the form its entries take, so the two can be compared."""
    said: tuple[str, ...] = ()
    """What each of its cards said of its dates, as the screenshot showed them."""
    lost: str | None = None
    """Why it asks again when its answer depended on a correction that wasn't kept."""


@dataclasses.dataclass(frozen=True)
class Question:
    """A question one destination asks, with nothing preselected that isn't stored."""

    field: str
    legend: str
    options: tuple[tuple[str, str, bool], ...]
    """(value, label, checked)."""
    many: bool = False
    helper: str | None = None
    unsaved: bool = False
    """Whether the correction in its box is typing this press sent that isn't saved."""
    text_label: str = ""
    text_type: str = "text"
    refused: str | None = None
    """A typed date Blossom can't read, said beneath its box rather than dropped."""
    mark: str | None = None
    leave: str | None = None
    """The "Leave this group unsaved" form a split question's row may be set aside with."""
    text: tuple[str, str] | None = None
    """For a field the reader wasn't sure of: the correction's field name and its typed value."""


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
class EntryShown:
    """One destination of the group preview, as text."""

    label: str
    cards: tuple[str, ...]
    lines: tuple[str, ...]
    still_open: str | None


@dataclasses.dataclass(frozen=True)
class UnitShown:
    """One unit of the group preview: its destinations, whether it can be ticked to save, what
    it waits for, its refusals and their offers, and its "Leave this group unsaved" form."""

    unit: str
    entries: tuple[EntryShown, ...]
    tick: bool
    ticked: bool
    """Ticked on the page, for display only: with saved progress, when its selection names every
    row of the group and the group is still open and can be ticked; with none, every group that
    can be ticked."""
    waits: tuple[str, ...]
    refusals: tuple[str, ...]
    offers: tuple[OfferShown, ...]
    leave: str


@dataclasses.dataclass(frozen=True)
class GroupShown:
    """A name group: its heading, its pieces' controls and its questions."""

    heading: str
    pieces: tuple[PieceControl, ...]
    questions: tuple[Question, ...]


@dataclasses.dataclass(frozen=True)
class NameQuestion:
    """The student-name question for screenshots whose name isn't her confirmed one."""

    field: str
    said: str
    options: tuple[tuple[str, str, bool], ...]
    remove: tuple[tuple[str, str], ...]
    """Each image "Remove only this screenshot" names, with its button's label."""


@dataclasses.dataclass(frozen=True)
class RowsShown:
    """The review's rows as the page shows them."""

    names: tuple[NameQuestion, ...]
    splits: tuple[Question, ...]
    groups: tuple[GroupShown, ...]
    preview: tuple[UnitShown, ...]
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


@dataclasses.dataclass(frozen=True)
class UnreadGroup:
    """Cards of one screenshot that couldn't be read for one reason: the group's heading, what
    it means, and each card with what was read of it and its "Set aside" form."""

    heading: str
    said: str
    cards: tuple[tuple[str, str], ...]
    """(the card's place and what was read of it, the id of its "Set aside" form)."""


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


def _preview_lines(
    state: ReviewState,
    entry: PreviewEntry,
    labels: Mapping[str, str],
    label: str,
    until: str,
    last_open: bool,
) -> list[str]:
    row = state.rows.get(entry.stands_for) if entry.stands_for else None
    members = [state.by_name[n] for n in entry.pieces]
    if entry.nothing_new and entry.defaulted_entries:
        return [f"Already saved in {label}. {SAID['nothing_new']}"]
    lines = []
    if entry.due is not None:
        lines.append(f"Due {day_text(entry.due)}.")
    lines.append(f"Assigned {day_text(entry.assigned)}." if entry.assigned else "No assigned date.")
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
    if entry.adds:
        lines += [f"Adds \u201c{words}\u201d" for words in entry.adds]
    else:
        lines.append(SAID["no_new_words"])
    if entry.nothing_new:
        lines.append(SAID["nothing_new"])
    if entry.written_kind in WRITTEN_KIND:
        lines.append(WRITTEN_KIND[entry.written_kind])
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
        lines.append(
            f"{label}: " + " ".join(_preview_lines(state, entry, labels, label, "", False))
        )
    fields = [("version", str(version)), ("press", "keep")]
    fields += [(answer_field("destination", n), v) for n, v in offer.changes]
    fields.append(("offer", page_check(page_key, fields)))
    form = OutsideForm(f"offer-{form_id}", "review", tuple(fields))
    return OfferShown(moves, tuple(lines), form.id), form


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
    return Question(answer_field("split", row.key), legend, tuple(options))


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
    corrections: Mapping[str, Mapping[str, str]],
    selected: frozenset[str] | None = None,
    open_keys: Sequence[str] = (),
    unread: Sequence[UnreadCard] = (),
    unsaved: frozenset[tuple[str, str]] = frozenset(),
    frozen: bool = False,
) -> RowsShown:
    """The review's rows: the name questions, the split questions, one control per open piece
    by name group, each destination's questions once, under the first name group with cards
    going there, and the group preview with its refusals and offers, plus the signed fields
    the review form carries."""
    values = dict(state.values)
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
    groups: dict[tuple[str, str], list[Piece]] = {}
    for piece in open_pieces:
        groups.setdefault(piece.pair, []).append(piece)
    previews = {e.dest: e for e in state.preview}
    asked_fields: list[tuple[str, str]] = [
        (q.field, json.dumps([o[0] for o in q.options], separators=(",", ":"))) for q in names
    ] + [(q.field, json.dumps([o[0] for o in q.options], separators=(",", ":"))) for q in splits]
    shown_groups = []
    asked_dests: set[tuple[str, ...]] = set()
    for pieces in groups.values():
        controls = tuple(
            PieceControl(
                p.name,
                piece_text(places, p),
                _choices(state, p, p.name in state.defaulted, labels),
                p.name,
                p.name in state.defaults and p.name not in state.stored,
                p.name in state.validation.of(p.name).stale,
                piece_read(p, pieces[0].course),
                dates_said(p, places),
                SAID["lost_correction"] if p.name in state.lost else None,
            )
            for p in pieces
        )
        for control in controls:
            asked_fields.append(
                (
                    answer_field("destination", control.name),
                    json.dumps([c.value for c in control.choices], separators=(",", ":")),
                )
            )
        questions: list[Question] = []
        dests = sorted(
            {
                d
                for d, names_there in state.validation.destinations.items()
                if set(names_there) & {p.name for p in pieces} and d not in asked_dests
            }
        )
        asked_dests.update(dests)
        for dest in dests:
            entry = previews.get(dest)
            if entry is None:
                continue
            label = _dest_label(state, entry, labels, pieces[0].title)
            unit = state.validation.of(entry.pieces[0])
            for q in unit.questions:
                if q.subject == subject_for(dest, entry.pieces):
                    questions.append(_date_question(state, q, label))
            for kq in unit.kind_questions:
                if kq.subject == subject_for(dest, entry.pieces):
                    got = values.get(("kind", kq.subject))
                    questions.append(
                        Question(
                            answer_field("kind", kq.subject),
                            f"Homework or task for {label}?",
                            (
                                ("HOMEWORK", SAID["homework"], got == "HOMEWORK"),
                                ("TASK", SAID["task"], got == "TASK"),
                            ),
                        )
                    )
            change = state.asked.get(dest)
            if change is not None:
                subject = instruction_subject(
                    dest, list(entry.pieces), change.instructions_revision
                )
                got = values.get(("instructions", subject))
                chosen = set(json.loads(got)) if got else set()
                options = tuple(
                    (s.text, f"\u201c{s.text}\u201d", s.text in chosen)
                    for s in change.instructions_shown
                )
                questions.append(
                    Question(
                        answer_field("instructions", subject),
                        f"Which instructions apply to {label}?",
                        (*options, ("", SAID["none_apply"], got == "[]")),
                        many=True,
                    )
                )
        typed: dict[str, Piece] = {}
        for piece in pieces:
            typed.setdefault(piece.key, piece)
        for key, piece in typed.items():
            got = values.get(("kind", key))
            questions.append(
                Question(
                    answer_field("kind", key),
                    f"{SAID['type']}: {piece_text(places, piece)}",
                    (
                        ("HOMEWORK", SAID["homework"], got == "HOMEWORK"),
                        ("TASK", SAID["task"], got == "TASK"),
                    ),
                    helper=SAID["type_helper"],
                )
            )
        for piece in pieces:
            if piece.due_choices and not any(
                typed_day(corrections.get(piece.key, {}).get(f) or "") for f in DATE_FIELDS
            ):
                got = values.get(("due_read", piece.name))
                questions.append(
                    Question(
                        answer_field("due_read", piece.name),
                        f"{piece_text(places, piece)}: {SAID['due_differs']}",
                        tuple(
                            (day.isoformat(), due_option(piece, day), got == day.isoformat())
                            for day in piece.due_choices
                        ),
                    )
                )
        confirming: dict[tuple[str, str], Piece] = {}
        for piece in pieces:
            for field in sorted(confirm_fields(piece)):
                confirming.setdefault((piece.key, field), piece)
        for (key, field), piece in confirming.items():
            questions.append(
                confirm_question(
                    piece, field, places, values, corrections.get(key, {}), (key, field) in unsaved
                )
            )
        for question in questions:
            offered = [o[0] for o in question.options]
            asked_fields.append((question.field, json.dumps(offered, separators=(",", ":"))))
        shown_groups.append(
            GroupShown(f"{pieces[0].course}: {pieces[0].title}", controls, tuple(questions))
        )
    digests = unit_digests(state)
    forms: list[OutsideForm] = []
    shown_units = []
    ordered = sorted(
        state.validation.units, key=lambda u: min(first_card(state.by_name[n]) for n in u.unit)
    )
    for number, unit in enumerate(ordered, 1):
        here = [e for e in state.preview if set(e.pieces) <= set(unit.unit)]
        last_open = set(unit.unit) >= {p.name for p in open_pieces}
        entries_shown = []
        still_any = False
        for entry in here:
            title = state.by_name[entry.pieces[0]].title
            label = _dest_label(state, entry, labels, title)
            lines = _preview_lines(state, entry, labels, label, until, last_open)
            members = [state.by_name[n] for n in entry.pieces]
            still = _still_open(entry, members, values, waiting, corrections)
            still_any = still_any or bool(still)
            entries_shown.append(
                EntryShown(
                    label,
                    tuple(piece_text(places, m) for m in members),
                    tuple(lines),
                    None
                    if not still
                    else "Still to answer: " + ", ".join(OPEN_NAMES[q] for q in still) + ".",
                )
            )
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
        shown_units.append(
            UnitShown(
                unit_name(unit.unit),
                tuple(entries_shown),
                tickable,
                tickable
                and (
                    selected is None or {state.by_name[name].key for name in unit.unit} <= selected
                ),
                _waits(unit),
                refusals,
                tuple(shown_offers),
                f"leave-{number}",
            )
        )
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
    for action in ("discard", "not-hers"):
        signed = [("version", str(version))]
        signed.append(("check", page_check(page_key, [*signed, ("action", action)])))
        forms.append(OutsideForm(action, action, tuple(signed)))
    return RowsShown(
        tuple(names),
        splits,
        tuple(shown_groups),
        tuple(shown_units),
        tuple(hidden),
        SAID["status"],
        (*forms, *unread_forms),
        bool(names) and not state.saved,
        unread_groups,
        tuple(piece_text(places, p) for p in state.pieces if p.name in state.saved),
    )


def _still_open(
    entry: PreviewEntry,
    members: Sequence[Piece],
    values: Mapping[tuple[str, str], str],
    waiting: set[str],
    corrections: Mapping[str, Mapping[str, str]],
) -> list[str]:
    """A destination's questions still open: its dates and kind, its instructions, a field the
    reader wasn't sure of, and the student name of a screenshot its cards came from."""
    still = list(entry.open_questions)
    if entry.instruction_revision is not None and entry.instruction_choice is None:
        still.append("instructions")
    if any(
        values.get(("confirm", confirm_subject(m.key, f))) is None
        and not corrections.get(m.key, {}).get(f)
        for m in members
        for f in confirm_fields(m)
    ):
        still.append("confirm")
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
