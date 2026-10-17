# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The homework screenshot review's rules, as pure functions of what one request reads.

A review's pieces come from its stored readings: one per row, or one per assigned day of a row
answered "These are different assignments", plus one for that row's undated cards. Each piece
gets one destination: a saved assignment of its class and title, a row an earlier pick linked
to its name, a row found under another name, or a numbered new slot. Pieces with the same
destination, and the pieces of one split row, are a unit, which saves whole or not at all.

Nothing here reads or writes a store. The record's own writes-nothing test, the instruction
question and the review's stored answers come in as arguments, so every function is a function
of its inputs alone.
"""

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any, Final, NamedTuple

import blossom.intake as intake
from blossom.intake import Reading
from blossom.reconciliation import SourceChannel
from blossom.school_instructions import InstructionSeen
from blossom.screenshots import (
    KEY_INSTANT,
    CardPlace,
    ScreenshotRow,
    cards_placed,
    homework_assembly,
)
from blossom.stores.import_drafts import Carry, StoredReading

WINDOW_DAYS: Final = 7
"""A saved assignment is listed for a piece when due within this many days of it, either side."""
MOST_OFFERS: Final = 5
"""The most offers a refusal shows, the move to new slots always among them."""
KINDS: Final = ("HOMEWORK", "TASK")
"""The kinds a destination's kind question chooses between."""

Destination = Mapping[str, Any]
"""A stored destination: ``{"existing": id}``, ``{"linked": id}``, ``{"found": id, "offer":
basis}`` or ``{"new": slot}``."""


# ------------------------------------------------------------------ what the rules read


@dataclasses.dataclass(frozen=True)
class SavedRow:
    """One saved assignment as the review compares pieces with it."""

    assignment_id: str
    course: str
    title: str
    due: date | None
    assigned: date | None
    words: frozenset[str]
    """Every school instruction kept under it, in any state, compared as whole texts."""
    first_words: str | None = None
    more_words: int = 0
    kind: str | None = None

    @property
    def pair(self) -> tuple[str, str]:
        """The class and title, folded as the record matches names."""
        return intake.pair(self.course, self.title)


@dataclasses.dataclass(frozen=True)
class Piece:
    """One piece of the review: its name, row key, class, title, dates and instruction words, and
    the reading and cards it came from."""

    name: str
    key: str
    course: str
    title: str
    due: date | None
    assigned: date | None
    words: frozenset[str]
    split: bool = False
    """Whether it is one assigned day of a row answered "These are different assignments"."""
    reading: Reading | None = dataclasses.field(default=None, compare=False, repr=False)
    cards: tuple[CardPlace, ...] = dataclasses.field(default=(), compare=False)
    uncertain: frozenset[str] = dataclasses.field(default=frozenset(), compare=False)
    seen: tuple["DatesSeen", ...] = dataclasses.field(default=(), compare=False)
    """What each of its cards said of its dates: the due line and the day heading as read."""
    due_resolved: bool = dataclasses.field(default=True, compare=False)
    """False while its due card's due line and day heading name different dates and nothing
    the parent gave settles which one Blossom uses."""

    @property
    def due_card(self) -> bool:
        """Whether it was read from due cards alone, so a day heading above it names its due
        date rather than the day it was assigned."""
        if self.seen:
            return all(one.card == "due" for one in self.seen)
        return self.assigned is None

    @property
    def due_choices(self) -> tuple[date, ...]:
        """The dates its due cards' due lines and day headings name, when they disagree."""
        days = {
            day
            for one in self.seen
            if one.card == "due"
            and one.line_day
            and one.heading_day
            and one.line_day != one.heading_day
            for day in (one.line_day, one.heading_day)
        }
        return tuple(sorted(days))

    @property
    def pair(self) -> tuple[str, str]:
        """The class and title, folded as the record matches names."""
        return intake.pair(self.course, self.title)


class DatesSeen(NamedTuple):
    """One card's dates as the screenshot showed them: which kind of card it is, its due line
    and its day heading as read, and the dates each names, if any."""

    place: CardPlace
    card: str
    """``due`` or ``assigned``."""
    line: str | None
    line_day: date | None
    heading: str | None
    heading_day: date | None


def dates_seen(
    readings: Sequence[StoredReading], cards: Sequence[CardPlace]
) -> tuple[DatesSeen, ...]:
    """What each card said of its dates, from the stored readings: its due line, and the day
    heading the assembly read it under. Both are kept whatever date the row uses."""
    placed = cards_placed(readings)
    out = []
    for place in cards:
        found = placed.get(place)
        if found is None or found.card is None:
            continue
        card, heading = found.card, found.heading
        out.append(
            DatesSeen(
                place,
                "assigned" if card.badge == "ASSIGNED" else "due",
                card.footer,
                card.due,
                None if heading is None else heading.text,
                None if heading is None else heading.day,
            )
        )
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class EarlierPick:
    """A ``renamed`` intake decision: cards of this class and title were saved to that row."""

    course: str
    title: str
    assignment_id: str
    sequence: int

    @property
    def pair(self) -> tuple[str, str]:
        """The class and title, folded as the record matches names."""
        return intake.pair(self.course, self.title)


# ------------------------------------------------------------------ destination values


def value_json(value: Destination) -> str:
    """The canonical JSON the answers table stores for a destination."""
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def value_of(text: str) -> dict[str, Any]:
    """A stored destination read back; anything else is ``ValueError``."""
    out = json.loads(text)
    if not isinstance(out, dict) or len({"existing", "linked", "found", "new"} & set(out)) != 1:
        msg = "not a destination"
        raise ValueError(msg)
    return out


def dest_kind(value: Destination) -> str:
    """Which of the four kinds a destination is."""
    for kind in ("existing", "linked", "found", "new"):
        if kind in value:
            return kind
    msg = "not a destination"
    raise ValueError(msg)


def dest_key(piece: Piece, value: Destination) -> tuple[str, ...]:
    """What makes two destinations one: one assignment id, or one slot of one name group."""
    kind = dest_kind(value)
    if kind == "new":
        return ("new", piece.course, piece.title, str(int(value["new"])))
    return ("id", str(value[kind]))


def assignment_of(value: Destination) -> str | None:
    """The assignment a destination names, or None for a new slot."""
    kind = dest_kind(value)
    return None if kind == "new" else str(value[kind])


# ------------------------------------------------------------------ the reference


def reference(assignment_id: str, others: Iterable[str]) -> str:
    """ "ref" and the last four characters after the id's last hyphen, two more at a time while
    another entry's agree, then the whole id, bare."""
    tail = assignment_id.rsplit("-", 1)[-1]
    rivals = [other.rsplit("-", 1)[-1] for other in others if other != assignment_id]
    size = 4
    while size < len(tail):
        mine = tail[-size:]
        if not any(rival[-size:] == mine for rival in rivals):
            return f"ref {mine}"
        size += 2
    if not any(rival == tail for rival in rivals):
        return f"ref {tail}"
    return assignment_id


# ------------------------------------------------------------------ the destination list


def in_window(piece_due: date | None, row_due: date | None) -> bool:
    """Due within the window of the piece's due date; a piece with no due date lists the
    same-name rows with none."""
    if piece_due is None or row_due is None:
        return piece_due is None and row_due is None
    return abs((row_due - piece_due).days) <= WINDOW_DAYS


@dataclasses.dataclass(frozen=True)
class ListEntry:
    """One line of a piece's destination list."""

    kind: str
    """``listed``, ``linked``, ``found_for_another``, ``find`` or ``new_slot``."""
    value: str
    stands_for: str | None = None
    slot: int | None = None


def listed(piece: Piece, rows: Iterable[SavedRow]) -> list[SavedRow]:
    """The saved assignments of the piece's class and title in its window, in a fixed order."""
    found = [r for r in rows if r.pair == piece.pair and in_window(piece.due, r.due)]
    return sorted(
        found,
        key=lambda r: (
            r.due or date.max,
            r.title,
            r.assigned or date.max,
            r.first_words or "",
            r.assignment_id,
        ),
    )


def linked(
    piece: Piece, picks: Iterable[EarlierPick], rows: Mapping[str, SavedRow]
) -> list[SavedRow]:
    """Each distinct row an earlier pick linked to the piece's name, newest decision first."""
    out: list[SavedRow] = []
    seen: set[str] = set()
    for pick in sorted(picks, key=lambda p: -p.sequence):
        if (
            pick.pair == piece.pair
            and pick.assignment_id in rows
            and pick.assignment_id not in seen
        ):
            seen.add(pick.assignment_id)
            row = rows[pick.assignment_id]
            if row.pair != piece.pair:
                out.append(row)
    return out


def slots_shown(group: Sequence[Piece], mapping: Mapping[str, Destination]) -> list[int]:
    """The slots the group's stored answers use, then one fresh slot per open piece after them."""
    used = sorted(
        {
            int(mapping[p.name]["new"])
            for p in group
            if p.name in mapping and "new" in mapping[p.name]
        }
    )
    top = max(used, default=0)
    return used + [top + i for i in range(1, len(group) + 1)]


def entries(
    piece: Piece,
    group: Sequence[Piece],
    rows: Mapping[str, SavedRow],
    picks: Iterable[EarlierPick],
    mapping: Mapping[str, Destination],
) -> list[ListEntry]:
    """The destination list: listed rows, linked rows, rows found for another piece of the
    group, "Find under another name", and the new slots."""
    out: list[ListEntry] = []
    listed_rows = listed(piece, rows.values())
    out += [
        ListEntry("listed", value_json({"existing": r.assignment_id}), r.assignment_id)
        for r in listed_rows
    ]
    shown = {r.assignment_id for r in listed_rows}
    for row in linked(piece, picks, rows):
        if row.assignment_id not in shown:
            out.append(
                ListEntry("linked", value_json({"linked": row.assignment_id}), row.assignment_id)
            )
            shown.add(row.assignment_id)
    for other in sorted(group, key=lambda p: p.name):
        value = mapping.get(other.name)
        if other.name == piece.name or value is None or "found" not in value:
            continue
        target = str(value["found"])
        if target not in shown and target in rows:
            out.append(ListEntry("found_for_another", value_json(value), target))
            shown.add(target)
    out.append(ListEntry("find", ""))
    out += [
        ListEntry("new_slot", value_json({"new": k}), None, k) for k in slots_shown(group, mapping)
    ]
    return out


def offerable(
    piece: Piece, value: Destination, rows: Mapping[str, SavedRow], picks: Iterable[EarlierPick]
) -> bool:
    """Whether a stored destination can still be offered to the piece."""
    kind = dest_kind(value)
    if kind == "new":
        return True
    target = str(value[kind])
    if target not in rows:
        return False
    if kind == "existing":
        row = rows[target]
        return row.pair == piece.pair and in_window(piece.due, row.due)
    if kind == "linked":
        return any(r.assignment_id == target for r in linked(piece, picks, rows))
    return True


# ------------------------------------------------------------------ facts beside each entry


@dataclasses.dataclass(frozen=True)
class EntryFact:
    """A fact shown beside an entry: whether it has the card's words, a differing date, or another
    listed assignment that also holds the card's words."""

    kind: str
    """``has_words``, ``lacks_words``, ``due_differs``, ``assigned_differs`` or
    ``also_saved_in``."""
    entry_date: date | None = None
    piece_date: date | None = None
    about: str | None = None


def holds_words(piece_words: frozenset[str], row_words: frozenset[str]) -> bool:
    """Whether every instruction text of the piece is among the row's; no words are always held."""
    return piece_words <= row_words


def in_list(
    piece: Piece, rows: Mapping[str, SavedRow], picks: Iterable[EarlierPick]
) -> list[SavedRow]:
    """The saved assignments the piece's list shows: listed rows and linked rows."""
    out = {r.assignment_id: r for r in listed(piece, rows.values())}
    for row in linked(piece, picks, rows):
        out.setdefault(row.assignment_id, row)
    return [out[key] for key in sorted(out)]


def facts(
    piece: Piece, row: SavedRow, rows: Mapping[str, SavedRow], picks: Iterable[EarlierPick] = ()
) -> list[EntryFact]:
    """The facts beside one entry. "Also saved in" names only assignments in the piece's list
    due on another date."""
    out = [EntryFact("has_words" if holds_words(piece.words, row.words) else "lacks_words")]
    if row.due != piece.due:
        out.append(EntryFact("due_differs", row.due, piece.due))
    if row.assigned is not None and piece.assigned is not None and row.assigned != piece.assigned:
        out.append(EntryFact("assigned_differs", row.assigned, piece.assigned))
    if piece.words:
        for other in in_list(piece, rows, picks):
            if (
                other.assignment_id != row.assignment_id
                and other.due != row.due
                and holds_words(piece.words, other.words)
            ):
                out.append(EntryFact("also_saved_in", about=other.assignment_id))
    return out


# ------------------------------------------------------------------ the defaults


def defaults(
    pieces: Sequence[Piece],
    groups: Mapping[tuple[str, str], Sequence[Piece]],
    rows: Mapping[str, SavedRow],
    picks: Iterable[EarlierPick],
    stored: Mapping[str, Destination],
    writes_nothing: Callable[[Piece, str], bool],
) -> dict[str, dict[str, Any]]:
    """The two preselections for pieces with no stored destination: "Already saved" for one exact
    match landing on which writes nothing, and a lone piece with nothing listed or linked on new
    slot 1. Two pieces never start on one assignment."""
    picks = list(picks)
    candidates: dict[str, str] = {}
    for piece in pieces:
        if piece.name in stored:
            continue
        options = listed(piece, rows.values()) + linked(piece, picks, rows)
        exact = sorted(
            {
                r.assignment_id
                for r in options
                if holds_words(piece.words, r.words)
                and r.due == piece.due
                and r.assigned == piece.assigned
            }
        )
        if len(exact) == 1 and writes_nothing(piece, exact[0]):
            candidates[piece.name] = exact[0]
    out: dict[str, dict[str, Any]] = {}
    by_target: dict[str, list[str]] = {}
    for name, target in candidates.items():
        by_target.setdefault(target, []).append(name)
    taken = {assignment_of(value) for value in stored.values()}
    for target, names in by_target.items():
        if len(names) == 1 and target not in taken:
            [name] = names
            piece = next(p for p in pieces if p.name == name)
            out[name] = {("existing" if rows[target].pair == piece.pair else "linked"): target}
    for group in groups.values():
        if len(group) != 1:
            continue
        [piece] = group
        if piece.name in stored or piece.name in out:
            continue
        if not listed(piece, rows.values()) and not linked(piece, picks, rows):
            out[piece.name] = {"new": 1}
    return out


# ------------------------------------------------------------------ units


def units(pieces: Sequence[Piece], mapping: Mapping[str, Destination]) -> list[tuple[str, ...]]:
    """The smallest sets closed under "same destination" and "one row's pieces"."""
    parent = {p.name: p.name for p in pieces}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    by_dest: dict[tuple[str, ...], str] = {}
    by_key: dict[str, str] = {}
    for piece in sorted(pieces, key=lambda p: p.name):
        if piece.name in mapping:
            key = dest_key(piece, mapping[piece.name])
            if key in by_dest:
                parent[find(piece.name)] = find(by_dest[key])
            else:
                by_dest[key] = piece.name
        if piece.key in by_key:
            parent[find(piece.name)] = find(by_key[piece.key])
        else:
            by_key[piece.key] = piece.name
    grouped: dict[str, list[str]] = {}
    for piece in pieces:
        grouped.setdefault(find(piece.name), []).append(piece.name)
    return sorted(tuple(sorted(names)) for names in grouped.values())


def unit_name(unit: Sequence[str]) -> str:
    """A unit's name: its sorted piece names joined by "+"."""
    return "+".join(sorted(unit))


# ------------------------------------------------------------------ dates and kind, per destination


@dataclasses.dataclass(frozen=True)
class DateQuestion:
    """A due or assigned date a destination asks for, with nothing preselected."""

    kind: str
    subject: str
    options: tuple[tuple[str, date], ...]
    """``(as_saved, day)`` for the saved date, ``(piece_date, day)`` for each piece's."""
    pieces: tuple[str, ...]


def subject_for(dest: tuple[str, ...], names: Iterable[str]) -> str:
    """A destination's question subject: the destination and its sorted piece names."""
    return json.dumps([list(dest), sorted(names)], separators=(",", ":"))


def date_questions(
    dest: tuple[str, ...], members: Sequence[Piece], row: SavedRow | None
) -> list[DateQuestion]:
    """The due date asked where the pieces differ or differ from the saved one, and the assigned
    date asked where a saved one differs from a piece's or the pieces differ."""
    out: list[DateQuestion] = []
    names = tuple(sorted(p.name for p in members))
    subject = subject_for(dest, names)
    dues = sorted({p.due for p in members if p.due is not None})
    if row is not None and row.due is not None and dues and (len(dues) > 1 or dues[0] != row.due):
        due_options = (("as_saved", row.due), *(("piece_date", d) for d in dues if d != row.due))
        out.append(DateQuestion("due", subject, due_options, names))
    elif len(dues) > 1:
        out.append(DateQuestion("due", subject, tuple(("piece_date", d) for d in dues), names))
    days = sorted({p.assigned for p in members if p.assigned is not None})
    if (
        row is not None
        and row.assigned is not None
        and days
        and (len(days) > 1 or days[0] != row.assigned)
    ):
        day_options = (
            ("as_saved", row.assigned),
            *(("piece_date", d) for d in days if d != row.assigned),
        )
        out.append(DateQuestion("assigned", subject, day_options, names))
    elif len(days) > 1:
        out.append(DateQuestion("assigned", subject, tuple(("piece_date", d) for d in days), names))
    return out


def written_dates(
    members: Sequence[Piece],
    row: SavedRow | None,
    questions: Sequence[DateQuestion],
    answers: Mapping[tuple[str, str], Any],
) -> tuple[date | None, date | None, bool]:
    """The due and assigned dates saving writes, and whether every date question is answered;
    an unanswered due date counts as the saved one."""
    ready = True
    due = (
        row.due
        if row is not None and row.due is not None
        else (members[0].due if members else None)
    )
    assigned = (
        row.assigned
        if row is not None and row.assigned is not None
        else next((p.assigned for p in members if p.assigned), None)
    )
    for question in questions:
        got = answers.get((question.kind, question.subject))
        chosen = next((d for _, d in question.options if d.isoformat() == got), None)
        if chosen is None:
            ready = False
        elif question.kind == "due":
            due = chosen
        else:
            assigned = chosen
    return due, assigned, ready


@dataclasses.dataclass(frozen=True)
class KindQuestion:
    """ "Homework or task?" for one destination, with nothing preselected."""

    subject: str
    pieces: tuple[str, ...]
    kind: str = "kind"


def kind_question(
    dest: tuple[str, ...], members: Sequence[Piece], row: SavedRow | None, kinds: Mapping[str, str]
) -> KindQuestion | None:
    """Asked when the pieces' rows chose different kinds, or a chosen kind differs from the saved
    assignment's. A kind left unchosen is no choice."""
    names = [p.name for p in members]
    chosen = {kinds[n] for n in names if n in kinds}
    if len(chosen) > 1 or (
        row is not None and row.kind is not None and any(k != row.kind for k in chosen)
    ):
        return KindQuestion(subject_for(dest, names), tuple(sorted(names)))
    return None


def destination_kind(
    dest: tuple[str, ...],
    members: Sequence[Piece],
    row: SavedRow | None,
    kinds: Mapping[str, str],
    answers: Mapping[tuple[str, str], Any],
) -> tuple[str | None, bool]:
    """The kind saving writes there, and whether its question, if any, is answered."""
    question = kind_question(dest, members, row, kinds)
    if question is None:
        chosen = {kinds[p.name] for p in members if p.name in kinds}
        return (next(iter(chosen)) if chosen else None), True
    got = answers.get(("kind", question.subject))
    return (got if got in KINDS else None), got in KINDS


# ------------------------------------------------------------------ copies from another assignment


def words_left(
    row: SavedRow,
    pieces: Sequence[Piece],
    mapping: Mapping[str, Destination],
    saved: Mapping[str, str],
) -> frozenset[str]:
    """The row's words less every word of this review's pieces saved or mapped to it."""
    brought: set[str] = set()
    for piece in pieces:
        if saved.get(piece.name) == row.assignment_id:
            brought |= piece.words
        value = mapping.get(piece.name)
        if value is not None and assignment_of(value) == row.assignment_id:
            brought |= piece.words
    return frozenset(row.words - brought)


def holders(
    piece: Piece,
    destination: str,
    dates: Iterable[date | None],
    rows: Mapping[str, SavedRow],
    pieces: Sequence[Piece],
    mapping: Mapping[str, Destination],
    saved: Mapping[str, str],
) -> list[str]:
    """Other same-name assignments due on the destination's dates whose words, less this
    review's, hold the piece's words."""
    wanted = set(dates)
    out = []
    for row in sorted(rows.values(), key=lambda r: r.assignment_id):
        if row.assignment_id == destination or row.pair != piece.pair or row.due not in wanted:
            continue
        if holds_words(piece.words, words_left(row, pieces, mapping, saved)):
            out.append(row.assignment_id)
    return out


# ------------------------------------------------------------------ validation


@dataclasses.dataclass(frozen=True)
class Refusal:
    """Why a unit can't save: two split pieces in one destination, or a copy of another
    assignment's words."""

    kind: str
    """``split_siblings`` or ``copy``."""
    pieces: tuple[str, ...]
    named: tuple[str, ...]
    """For a copy: the destination and the assignment whose words it would copy."""


@dataclasses.dataclass(frozen=True)
class UnitCheck:
    """One unit's refusals, open questions and readiness."""

    unit: tuple[str, ...]
    refusals: tuple[Refusal, ...]
    questions: tuple[DateQuestion, ...]
    ready: bool
    unanswered: tuple[str, ...]
    stale: tuple[str, ...]
    kind_questions: tuple[KindQuestion, ...] = ()


@dataclasses.dataclass(frozen=True)
class Validation:
    """Every unit's check, and each destination's pieces."""

    units: tuple[UnitCheck, ...]
    destinations: dict[tuple[str, ...], tuple[str, ...]]

    def of(self, name: str) -> UnitCheck:
        """The check of the unit holding this piece."""
        return next(u for u in self.units if name in u.unit)


def validate(
    pieces: Sequence[Piece],
    mapping: Mapping[str, Destination],
    rows: Mapping[str, SavedRow],
    picks: Iterable[EarlierPick],
    saved: Mapping[str, str],
    answers: Mapping[tuple[str, str], Any],
    instruction_ready: Callable[[tuple[str, ...], Sequence[Piece]], bool] = lambda d, m: True,
    kinds: Mapping[str, str] | None = None,
) -> Validation:
    """Each unit's refusals and open questions, as one pure function of the mapping and the
    record. Saved pieces aren't checked, but their words count when telling a copy."""
    picks = list(picks)
    kinds = dict(kinds or {})
    by_name = {p.name: p for p in pieces}
    open_pieces = [p for p in pieces if p.name not in saved]
    live: dict[str, Destination] = {}
    stale: set[str] = set()
    for piece in open_pieces:
        value = mapping.get(piece.name)
        if value is None:
            continue
        if offerable(piece, value, rows, picks):
            live[piece.name] = value
        else:
            stale.add(piece.name)
    dests: dict[tuple[str, ...], list[str]] = {}
    for name, value in sorted(live.items()):
        dests.setdefault(dest_key(by_name[name], value), []).append(name)
    results = []
    for unit in units(open_pieces, live):
        refusals: list[Refusal] = []
        questions: list[DateQuestion] = []
        kind_questions: list[KindQuestion] = []
        unanswered = tuple(n for n in unit if n not in live)
        ready = not unanswered and not any(n in stale for n in unit)
        for dest in sorted({dest_key(by_name[n], live[n]) for n in unit if n in live}):
            members = [by_name[n] for n in dests[dest]]
            siblings: dict[str, list[str]] = {}
            for member in members:
                if member.split:
                    siblings.setdefault(member.key, []).append(member.name)
            for names in siblings.values():
                if len(names) > 1:
                    refusals.append(Refusal("split_siblings", tuple(sorted(names)), ()))
            row = rows.get(dest[1]) if dest[0] != "new" else None
            asked = date_questions(dest, members, row)
            questions += asked
            due, _, dates_ready = written_dates(members, row, asked, answers)
            ready = ready and dates_ready
            kind_asked = kind_question(dest, members, row, kinds)
            if kind_asked is not None:
                kind_questions.append(kind_asked)
                ready = ready and answers.get(("kind", kind_asked.subject)) in KINDS
            ready = ready and instruction_ready(dest, members)
            if row is not None:
                for member in sorted(members, key=lambda p: p.name):
                    if holds_words(member.words, row.words):
                        continue
                    found = holders(
                        member, row.assignment_id, {row.due, due}, rows, pieces, live, saved
                    )
                    if found:
                        refusals.append(
                            Refusal("copy", (member.name,), (row.assignment_id, found[0]))
                        )
        results.append(
            UnitCheck(
                tuple(unit),
                tuple(refusals),
                tuple(questions),
                ready and not refusals,
                unanswered,
                tuple(sorted(stale & set(unit))),
                tuple(kind_questions),
            )
        )
    return Validation(tuple(results), {d: tuple(n) for d, n in dests.items()})


# ------------------------------------------------------------------ offers


def fresh_slots(
    pieces: Sequence[Piece], mapping: Mapping[str, Destination], name: tuple[str, str], count: int
) -> list[int]:
    """New slots of a name group numbered after the highest the mapping uses."""
    pairs = {p.name: p.pair for p in pieces}
    used = {int(v["new"]) for n, v in mapping.items() if "new" in v and pairs.get(n) == name}
    top = max(used, default=0)
    return [top + i for i in range(1, count + 1)]


@dataclasses.dataclass(frozen=True)
class Offer:
    """A change of destinations that clears a refusal, shown whole and stored only when posted."""

    changes: tuple[tuple[str, str], ...]
    """(piece name, destination JSON), sorted by piece name."""
    moved_from: tuple[tuple[str, str | None], ...]


def closure(
    pieces: Sequence[Piece],
    mapping: Mapping[str, Destination],
    unit: UnitCheck,
    check: Callable[[Mapping[str, Destination]], Validation],
) -> dict[str, Destination]:
    """Every piece a refusal names moved to a new slot of its own, again for any unit that
    becomes refused, until none is."""
    by_name = {p.name: p for p in pieces}
    moved_mapping: dict[str, Destination] = {k: dict(v) for k, v in mapping.items()}
    todo = sorted({n for r in unit.refusals for n in r.pieces})
    moved: set[str] = set()
    for _ in range(len(pieces) + 1):
        for name in todo:
            if name not in moved:
                [slot] = fresh_slots(pieces, moved_mapping, by_name[name].pair, 1)
                moved_mapping[name] = {"new": slot}
                moved.add(name)
        result = check(moved_mapping)
        todo = sorted({n for u in result.units for r in u.refusals for n in r.pieces} - moved)
        if not todo:
            return moved_mapping
    msg = "moving refused pieces to new slots didn't end"
    raise AssertionError(msg)


def offers(
    pieces: Sequence[Piece],
    mapping: Mapping[str, Destination],
    unit: UnitCheck,
    entry_values: Mapping[str, Sequence[str]],
    check: Callable[[Mapping[str, Destination]], Validation],
) -> list[Offer]:
    """Changes that clear the refused unit and refuse no other, the move to new slots always
    among them, fewest pieces changed first; at most ``MOST_OFFERS``."""
    refused_before = {u.unit for u in check(mapping).units if u.refusals}

    def good(changed: Mapping[str, Destination]) -> bool:
        return not any(
            u.refusals and (set(u.unit) & set(unit.unit) or u.unit not in refused_before)
            for u in check(changed).units
        )

    def as_offer(changed: Mapping[str, Destination]) -> Offer:
        names = sorted(n for n in set(changed) | set(mapping) if changed.get(n) != mapping.get(n))
        return Offer(
            tuple((n, value_json(changed[n])) for n in names),
            tuple((n, value_json(mapping[n]) if n in mapping else None) for n in names),
        )

    out: list[Offer] = []
    for name in sorted({n for r in unit.refusals for n in r.pieces}):
        for value in entry_values.get(name, ()):
            if not value or (name in mapping and value == value_json(mapping[name])):
                continue
            changed: dict[str, Destination] = {k: dict(x) for k, x in mapping.items()}
            changed[name] = value_of(value)
            if good(changed):
                out.append(as_offer(changed))
    moved = as_offer(closure(pieces, mapping, unit, check))
    ranked = sorted({o for o in out if o != moved}, key=lambda o: (len(o.changes), o.changes))
    return sorted([*ranked[: MOST_OFFERS - 1], moved], key=lambda o: (len(o.changes), o.changes))


# ------------------------------------------------------------------ new-slot ids


def _slot_tag(course: str, title: str, names: Sequence[str], salt: int) -> str:
    digest = hashlib.sha256(json.dumps([course, title, sorted(names), salt]).encode()).hexdigest()
    return "s4:" + digest[:16]


def slot_occurrence(course: str, title: str, names: Iterable[str], on_record: Iterable[str]) -> str:
    """The occurrence tag of a new slot's assignment: over the class, the title and the sorted
    piece names, with the smallest salt whose id isn't on record."""
    taken = set(on_record)
    pieces = sorted(names)
    salt = 0
    while intake.identity(course, title, _slot_tag(course, title, pieces, salt)) in taken:
        salt += 1
    return _slot_tag(course, title, pieces, salt)


def slot_id(course: str, title: str, names: Iterable[str], on_record: Iterable[str]) -> str:
    """The id a new slot's assignment is saved under. No random value enters it."""
    return intake.identity(course, title, slot_occurrence(course, title, names, on_record))


# ------------------------------------------------------------------ the group preview


@dataclasses.dataclass(frozen=True)
class PreviewEntry:
    """One destination of the group preview, with the digest a save rechecks."""

    unit: tuple[str, ...]
    dest: tuple[str, ...]
    kind: str
    """``existing``, ``linked``, ``found`` or ``new``."""
    stands_for: str | None
    pieces: tuple[str, ...]
    due: date | None
    assigned: date | None
    adds: tuple[str, ...]
    nothing_new: bool
    open_questions: tuple[str, ...]
    overlap: tuple[str, ...]
    defaulted_entries: tuple[tuple[str, tuple[str, ...]], ...]
    instruction_revision: int | None
    instruction_choice: tuple[str, ...] | None
    digest: str
    written_kind: str | None = None
    kind_from: str | None = None


def digest_of(fields: Mapping[str, Any]) -> str:
    """The SHA-256 of a preview entry's fields in canonical JSON."""
    text = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def preview(
    validation: Validation,
    pieces: Sequence[Piece],
    mapping: Mapping[str, Destination],
    rows: Mapping[str, SavedRow],
    answers: Mapping[tuple[str, str], Any],
    defaulted: Iterable[str],
    entry_ids: Mapping[str, Sequence[str]],
    instructions: Callable[
        [tuple[str, ...], Sequence[Piece]], tuple[int | None, tuple[str, ...] | None]
    ] = lambda d, m: (None, None),
    picks: Iterable[EarlierPick] = (),
    kinds: Mapping[str, str] | None = None,
    writes_nothing: Callable[[Piece, str], bool] = lambda p, t: True,
) -> list[PreviewEntry]:
    """One entry per chosen destination. "Nothing new" holds only when saving writes nothing at
    all there: no words, no date moved or filled, no kind, no open question."""
    picks = list(picks)
    kinds = dict(kinds or {})
    by_name = {p.name: p for p in pieces}
    defaulted = set(defaulted)
    out = []
    for dest, names in sorted(validation.destinations.items()):
        members = [by_name[n] for n in names]
        unit = validation.of(names[0])
        target = None if dest[0] == "new" else dest[1]
        row = rows.get(target) if target else None
        asked = [q for q in unit.questions if q.subject == subject_for(dest, names)]
        due, assigned, _ = written_dates(members, row, asked, answers)
        words = frozenset().union(*(m.words for m in members))
        adds = tuple(sorted(words - (row.words if row else frozenset())))
        written_kind, _ = destination_kind(dest, members, row, kinds, answers)
        kind_asked = kind_question(dest, members, row, kinds)
        kind_change = row is not None and written_kind is not None and written_kind != row.kind
        revision, choice = instructions(dest, members)
        open_questions = tuple(q.kind for q in asked if (q.kind, q.subject) not in answers)
        if kind_asked is not None and answers.get(("kind", kind_asked.subject)) not in KINDS:
            open_questions += ("kind",)
        nothing_new = (
            row is not None
            and not adds
            and due == row.due
            and assigned == row.assigned
            and not kind_change
            and not open_questions
            and not (revision is not None and choice is None)
            and all(writes_nothing(m, row.assignment_id) for m in members)
        )
        overlap = tuple(
            sorted(
                {
                    r.assignment_id
                    for m in members
                    if m.words
                    for r in in_list(m, rows, picks)
                    if r.assignment_id != target and holds_words(m.words, r.words)
                }
            )
        )
        defaults_here = tuple(
            (n, tuple(entry_ids.get(n, ()))) for n in sorted(names) if n in defaulted
        )
        fields = {
            "dest": list(dest),
            "pieces": sorted(names),
            "due": due,
            "assigned": assigned,
            "adds": list(adds),
            "row": None if row is None else [row.due, row.assigned, sorted(row.words)],
            "revision": revision,
            "choice": None if choice is None else sorted(choice),
            "defaulted": [[n, list(ids)] for n, ids in defaults_here],
            "kind": written_kind,
            "kind_asked": kind_asked is not None,
        }
        out.append(
            PreviewEntry(
                unit.unit,
                dest,
                dest_kind(mapping[names[0]]),
                target,
                tuple(sorted(names)),
                due,
                assigned,
                adds,
                nothing_new,
                open_questions,
                overlap,
                defaults_here,
                revision,
                choice,
                digest_of(fields),
                written_kind,
                None if row is None else row.kind,
            )
        )
    return out


# ------------------------------------------------------------------ pieces from the readings

_DAY = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def typed_day(value: str) -> date | None:
    """A typed date, in ISO form or as the portal writes it (month/day/year); else None."""
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        found = _DAY.search(value)
        if found is None:
            return None
        try:
            return date(int(found.group(3)), int(found.group(1)), int(found.group(2)))
        except ValueError:
            return None


def corrected(reading: Reading, fields: Mapping[str, str] | None) -> Reading:
    """The reading with the parent's typed corrections on its uncertain fields: class, title,
    instruction words, the due line, or the day heading (the assigned date on an assigned
    card's row, else the due date). A value that can't be read as its field is passed over."""
    if not fields:
        return reading
    change: dict[str, Any] = {}
    for field in ("course", "title"):
        value = (fields.get(field) or "").strip()
        if value:
            change[field] = value
    words = (fields.get("instruction") or "").strip()
    if words:
        seen = [dataclasses.replace(i, text=words) for i in reading.instructions]
        seen = seen or [InstructionSeen(words, SourceChannel.LMS)]
        change["instructions"] = tuple({i.text: i for i in seen}.values())
    due = typed_day(fields["footer"]) if fields.get("footer") else None
    if fields.get("heading"):
        day = typed_day(fields["heading"])
        if day is not None and reading.assigned_on is not None:
            change["assigned_on"] = day
        elif day is not None:
            due = day
    if due is not None and due != reading.due_date:
        before = None if reading.due_date is None else reading.due_date.isoformat()
        change["due_date"] = due
        change["claims"] = tuple(
            c.model_copy(update={"asserted_value": due.isoformat()})
            if c.asserted_value == before
            else c
            for c in reading.claims
        )
    return dataclasses.replace(reading, **change) if change else reading


def _piece(
    name: str,
    key: str,
    reading: Reading,
    cards: Sequence[CardPlace],
    split: bool,
    uncertain: frozenset[str],
) -> Piece:
    return Piece(
        name,
        key,
        reading.course,
        reading.title,
        reading.due_date,
        reading.assigned_on,
        frozenset(i.text for i in reading.instructions),
        split,
        reading,
        tuple(cards),
        frozenset(uncertain),
    )


def pieces_from(
    readings: Sequence[StoredReading],
    split: Mapping[str, Any],
    aside: frozenset[str],
    now: datetime,
    corrections: Mapping[str, Mapping[str, str]] | None = None,
    due_read: Mapping[str, str] | None = None,
) -> tuple[list[Piece], list[ScreenshotRow], list[ScreenshotRow]]:
    """The review's pieces, the rows still asking the split question, and every row. A row
    answered "These are different assignments" gives one piece per assigned day and one for its
    undated cards, each read from its own cards and dated by them alone."""
    rows = list(homework_assembly(list(readings), now=now).rows)
    corrections = corrections or {}
    out: list[Piece] = []
    asking: list[ScreenshotRow] = []

    def made(
        name: str,
        key: str,
        reading: Reading,
        cards: Sequence[CardPlace],
        is_split: bool,
        marks: frozenset[str],
    ) -> Piece:
        return _piece(name, key, corrected(reading, corrections.get(key)), cards, is_split, marks)

    def own(
        row: ScreenshotRow, cards: Sequence[CardPlace], name: str, is_split: bool, day: date | None
    ) -> Piece:
        mine = [r for r in readings if r.sha256 in {c.sha256 for c in cards}]
        found = [r for r in homework_assembly(mine, now=now).rows if set(cards) & set(r.sources)]
        best = max(found, key=lambda r: len(set(cards) & set(r.sources)))
        reading = dataclasses.replace(best.reading, assigned_on=day)
        return made(name, row.key, reading, cards, is_split, best.marks)

    for row in rows:
        if row.key in aside:
            continue
        if not row.assigned_dates_disagree:
            out.append(made(row.key, row.key, row.reading, row.sources, False, row.marks))
            continue
        answer = split.get(row.key)
        if answer is None:
            asking.append(row)
        elif answer == "split":
            for day in row.assigned:
                out.append(own(row, day.sources, f"{row.key}|{day.day.isoformat()}", True, day.day))
            dated = {c for day in row.assigned for c in day.sources}
            undated = [c for c in row.sources if c not in dated]
            if undated:
                out.append(own(row, undated, f"{row.key}|due", False, None))
        else:
            chosen = date.fromisoformat(answer[1])
            reading = dataclasses.replace(row.reading, assigned_on=chosen)
            out.append(made(row.key, row.key, reading, row.sources, False, row.marks))
    settled = [_settled(piece, readings, corrections, due_read or {}) for piece in out]
    return sorted(settled, key=lambda p: p.name), asking, rows


def _settled(
    piece: Piece,
    readings: Sequence[StoredReading],
    corrections: Mapping[str, Mapping[str, str]],
    due_read: Mapping[str, str],
) -> Piece:
    """A piece with what its cards said of their dates. Where a due card's due line and day
    heading name different dates, the parent's answer of which one Blossom uses sets the due
    date, as a typed due date does; until one of them is given the piece isn't settled. The
    row's key is never touched."""
    seen = dates_seen(readings, piece.cards)
    with_seen = dataclasses.replace(piece, seen=seen)
    choices = with_seen.due_choices
    if not choices:
        return with_seen
    typed = corrections.get(piece.key, {})
    if any(typed_day(typed.get(field) or "") for field in ("footer", "heading")):
        return with_seen
    chosen = next((d for d in choices if d.isoformat() == due_read.get(piece.name)), None)
    if chosen is None or piece.reading is None:
        return dataclasses.replace(with_seen, due_resolved=False)
    before = None if piece.due is None else piece.due.isoformat()
    reading = dataclasses.replace(
        piece.reading,
        due_date=chosen,
        claims=tuple(
            c.model_copy(update={"asserted_value": chosen.isoformat()})
            if c.asserted_value == before
            else c
            for c in piece.reading.claims
        ),
    )
    return dataclasses.replace(with_seen, due=chosen, reading=reading)


def piece_names(
    readings: Sequence[StoredReading], split: Mapping[str, str]
) -> dict[str, frozenset[str]]:
    """Each row key's piece names, for the draft store: split answers come as stored JSON."""
    answers = {key: json.loads(value) for key, value in split.items()}
    pieces, _, _ = pieces_from(readings, answers, frozenset(), KEY_INSTANT)
    out: dict[str, set[str]] = {}
    for piece in pieces:
        out.setdefault(piece.key, set()).add(piece.name)
    return {key: frozenset(names) for key, names in out.items()}


# ------------------------------------------------------------------ a change of readings


def copy_group(row: ScreenshotRow) -> str:
    """A row's identical-copy group: its reading without the occurrence, and its assigned days."""
    reading = dataclasses.replace(row.reading, occurrence=None)
    text = repr(reading) + repr([day.day for day in row.assigned])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class Cleared:
    """What a change of readings clears and carries, with a note per row it explains."""

    delete: frozenset[tuple[str, str]]
    marks: frozenset[str]
    aside_insert: frozenset[str]
    aside_delete: frozenset[str]
    notes: tuple[tuple[str, str], ...]
    """(row key, ``aside_carried`` or ``asks_again``)."""


def answer_names(kind: str, subject: str) -> list[str]:
    """The piece names an answer is about; a split answer's row key for a split answer."""
    if kind == "kind" and subject.startswith("["):
        return list(json.loads(subject)[1])
    if kind in ("destination", "split", "kind", "due_read"):
        return [subject]
    if kind in ("due", "assigned", "instructions"):
        return list(json.loads(subject)[1])
    return []


def cleared(
    old_rows: Sequence[ScreenshotRow],
    new_rows: Sequence[ScreenshotRow],
    answers: Mapping[tuple[str, str], Any],
    aside: frozenset[str],
) -> Cleared:
    """A piece's destination is cleared, and the piece marked, when its row is gone or its
    identical-copy group changes; so are the date, kind and instruction answers of any
    destination holding such a piece, and the split answer of such a row. Rows set aside carry
    to rows made of their cards alone, and rows with new cards ask again."""
    old_keys = {r.key for r in old_rows}
    gone = old_keys - {r.key for r in new_rows}

    def images(rows: Sequence[ScreenshotRow]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for row in rows:
            out.setdefault(copy_group(row), set()).update(c.sha256 for c in row.sources)
        return out

    before, after = images(old_rows), images(new_rows)
    touched: set[str] = set()
    groups: dict[str, list[str]] = {}
    for row in new_rows:
        groups.setdefault(copy_group(row), []).append(row.key)
    for group, keys in groups.items():
        count = len([r for r in old_rows if copy_group(r) == group])
        if len(keys) + count > 1 and before.get(group) != after.get(group):
            touched |= set(keys)

    def hit(name: str) -> bool:
        return name.split("|")[0] in gone or name.split("|")[0] in touched

    delete: set[tuple[str, str]] = set()
    marks: set[str] = set()
    for kind, subject in sorted(answers):
        if kind == "split":
            if subject in gone or subject in touched:
                delete.add((kind, subject))
        elif kind == "destination":
            if hit(subject):
                delete.add((kind, subject))
                marks.add(subject)
        elif (
            kind in ("due", "assigned", "instructions")
            or (kind == "kind" and subject.startswith("["))
        ) and any(hit(name) for name in answer_names(kind, subject)):
            delete.add((kind, subject))
    notes: list[tuple[str, str]] = []
    carried: set[str] = set()
    asked: set[str] = set()
    aside_cards = {c for r in old_rows if r.key in aside for c in r.sources}
    for row in new_rows:
        if row.key in old_keys and row.key not in touched:
            continue
        cards = set(row.sources)
        if cards and cards <= aside_cards:
            if row.key not in aside:
                carried.add(row.key)
            notes.append((row.key, "aside_carried"))
        elif aside_cards & cards or row.key in aside:
            if row.key in aside:
                asked.add(row.key)
            notes.append((row.key, "asks_again"))
    return Cleared(
        frozenset(delete), frozenset(marks), frozenset(carried), frozenset(asked), tuple(notes)
    )


def carry(
    before: Sequence[StoredReading],
    after: Sequence[StoredReading],
    answers: Mapping[tuple[str, str], str],
    aside: frozenset[str],
) -> Carry:
    """What a change of readings does to the review's answers and set-aside rows, for the draft
    store's change transaction."""
    old_rows = homework_assembly(list(before), now=KEY_INSTANT).rows
    new_rows = homework_assembly(list(after), now=KEY_INSTANT).rows
    result = cleared(old_rows, new_rows, answers, aside)
    return Carry(result.delete, result.marks, result.aside_insert, result.aside_delete)
