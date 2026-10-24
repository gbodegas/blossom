# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Reading a screenshot review's posts against what their page signed.

The review form carries its answers, its boxes of corrections and ticked groups, and signs its
hidden fields, among them every question and box it offered with the values it offered. A box
is a correction exactly when it differs from its value with no correction, so a page posted
untouched corrects nothing and an earlier correction left in its box is posted again. An
offer, or a row found under another name, is a whole change of its own: a form whose every
field is signed. A post that doesn't match what its page signed is refused whole, so nothing
of it is kept.
"""

import dataclasses
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Final, NamedTuple

from blossom.screenshot_page import page_check, same_signature
from blossom.screenshot_review import (
    OLD_DATES,
    PIECE_DATES,
    ROW_FIELDS,
    Corrections,
    answer_names,
    dates_key,
    dest_kind,
    field_most,
    instruction_index,
    typed_day,
    usable_correction,
    value_json,
    value_of,
)
from blossom.screenshot_save import (
    ReviewState,
    box_texts,
    canonical,
    correctable,
    correction_digest,
    correction_text,
    fold_reads,
    piece_digest,
    read_digest,
    wordless_rows,
)
from blossom.stores.import_drafts import AnswerToKeep

PRESSES: Final = frozenset({"keep", "save", "later"})
ANSWER_KINDS: Final = frozenset(
    {
        "destination",
        "split",
        "due",
        "assigned",
        "kind",
        "instructions",
        "name",
        "confirm",
        "due_read",
    }
)
SIGNED: Final = ("version", "progress_revision", "acceptance", "asked", "defaults", "identity")
"""The review form's signed fields, besides each group's ``digest:{unit}``, in its order."""


class PostRefused(Exception):
    """A post that doesn't match what its page signed, or asks what the page didn't: nothing of
    it is kept."""


@dataclasses.dataclass(frozen=True)
class ReviewPost:
    """A review post as read: which press, the page's version, its acceptance id and saved
    progress revision, the answers to keep, the typed corrections, and the groups ticked with
    the digests and defaults the page showed."""

    press: str
    version: int
    acceptance: str
    progress_revision: int
    answers: tuple[AnswerToKeep, ...]
    corrections: dict[str, dict[str, str | None]]
    units: tuple[str, ...]
    digests: dict[str, tuple[str, ...]]
    defaults: dict[str, str]
    withdrawn: tuple[tuple[str, str], ...] = ()
    """Each confirmation, instruction choice or "These are her homework" the page asked for and
    the press left blank, by kind and subject: a box left unticked takes back the answer it
    held."""
    ticked: bool = True
    """Whether the press posted the page's ticks; an offer or a found row posts none."""
    too_long: tuple[tuple[str, str], ...] = ()
    """Each correction, by key and field, longer than its field takes: the press is refused
    whole, and the correction goes back in its box as typed."""
    needed: tuple[tuple[str, str], ...] = ()
    """Each class, title or settled due date, by key and field, whose box was cleared: the press
    is refused whole, and the box goes back empty."""
    both: tuple[str, ...] = ()
    """Each piece's dates key whose assigned date was typed beside "No assigned date was
    given": the press is refused whole, and both go back as posted."""

    @property
    def refused(self) -> bool:
        """Whether the press is refused whole for a box it can't take as posted."""
        return bool(self.too_long or self.needed or self.both)


WITHDRAWABLE: Final = ("answer:confirm:", "answer:instructions:", "answer:name:missing")
"""The answer fields, by prefix, a press takes back when it posts none of their boxes: each is
a set of checkboxes, so a box left unticked sends nothing."""


def _row_of(state: ReviewState, kind: str, subject: str) -> str | None:
    """The row key an answer's corrections belong to."""
    if kind == "name":
        return None
    if kind in ("split", "kind") and not subject.startswith("["):
        return subject
    if kind == "confirm":
        return str(json.loads(subject)[0])
    names = answer_names(kind, subject)
    piece = state.by_name.get(names[0]) if names else None
    return None if piece is None else piece.key


def _names_of(state: ReviewState, kind: str, subject: str) -> tuple[str, ...]:
    """The piece names an answer is about, so one for pieces another save took is dropped."""
    if kind == "confirm":
        key = str(json.loads(subject)[0])
        return tuple(p.name for p in state.pieces if p.key == key)
    if kind == "split" or (kind == "kind" and not subject.startswith("[")):
        return tuple(p.name for p in state.pieces if p.key == subject)
    return tuple(answer_names(kind, subject))


def _answer(
    state: ReviewState,
    kind: str,
    subject: str,
    value: str,
    corrections: Corrections,
) -> AnswerToKeep:
    piece = state.by_name.get(subject) if kind == "destination" else None
    if piece is not None:
        digest = piece_digest(piece, corrections)
    else:
        row = _row_of(state, kind, subject)
        digest = correction_digest(None if row is None else corrections.get(row))
    return AnswerToKeep(kind, subject, value, digest, _names_of(state, kind, subject))


def _single(items: Sequence[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in items:
        if name in out:
            raise PostRefused
        out[name] = value
    return out


def _destination(value: str) -> str:
    try:
        chosen = value_of(value)
        kind = dest_kind(chosen)
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as error:
        raise PostRefused from error
    if kind not in ("existing", "linked", "found", "new"):
        raise PostRefused
    return value_json(chosen)


def read_change(
    items: Sequence[tuple[str, str]],
    page_key: bytes,
    state: ReviewState,
    corrections: Corrections,
) -> ReviewPost:
    """An offer's or a found row's post: every field signed, destination answers alone, kept
    as a press of Keep these answers, with the corrections they were given with."""
    fields = _single(items)
    signature = fields.pop("offer", "")
    if not same_signature(signature, page_check(page_key, sorted(fields.items()))):
        raise PostRefused
    if fields.pop("press", "") != "keep":
        raise PostRefused
    version = _number(fields.pop("version", ""))
    answers = []
    for name, value in sorted(fields.items()):
        kind, subject = _field(name)
        if kind != "destination" or subject not in state.by_name:
            raise PostRefused
        answers.append(_answer(state, kind, subject, _destination(value), corrections))
    if not answers:
        raise PostRefused
    typed = {key: dict(fields) for key, fields in corrections.items()}
    return ReviewPost("keep", version, "", 0, tuple(answers), typed, (), {}, {}, ticked=False)


def _number(text: str) -> int:
    if not text.isdigit() or len(text) > 12:
        raise PostRefused
    return int(text)


def _field(name: str) -> tuple[str, str]:
    parts = name.split(":", 2)
    if len(parts) != 3 or parts[0] != "answer" or parts[1] not in ANSWER_KINDS or not parts[2]:
        raise PostRefused
    return parts[1], parts[2]


def read_review(
    items: Sequence[tuple[str, str]], page_key: bytes, state: ReviewState
) -> ReviewPost:
    """The review form's post: its signed fields checked, each answer one the page asked with a
    value it offered, each box one the page offered, each ticked group one it showed. A
    confirmation or instruction question the page asked and the press left blank is
    withdrawn. Each box is compared with its value with no correction and kept as a
    correction only where it differs (``box_corrections``). A date Blossom couldn't read,
    carried back beside its empty date box, stays the box's correction until a date is chosen
    there. A correction longer than its field takes is named in ``too_long`` and kept as typed,
    never cut, for the page to give back."""
    signed = [(n, v) for n, v in items if n in SIGNED or n.startswith("digest:") or n == "check"]
    hidden = _single(signed)
    check = hidden.pop("check", "")
    if not same_signature(check, page_check(page_key, list(hidden.items()))):
        raise PostRefused
    if any(n not in hidden for n in SIGNED):
        raise PostRefused
    try:
        offered = {str(f): set(json.loads(v)) for f, v in json.loads(hidden["asked"])}
        defaults = {str(k): str(v) for k, v in json.loads(hidden["defaults"]).items()}
    except (ValueError, TypeError, AttributeError) as error:
        raise PostRefused from error
    digests = {
        n.removeprefix("digest:"): tuple(v.split(",")) if v else ()
        for n, v in hidden.items()
        if n.startswith("digest:")
    }
    rest = [(n, v) for n, v in items if (n, v) not in signed]
    presses = [v for n, v in rest if n == "press"]
    if len(presses) != 1 or presses[0] not in PRESSES:
        raise PostRefused
    units = tuple(v for n, v in rest if n == "unit")
    if len(set(units)) != len(units) or any(u not in digests for u in units):
        raise PostRefused
    boxes: dict[tuple[str, str], str] = {}
    carried: dict[tuple[str, str], str] = {}
    none_given: set[str] = set()
    chosen: dict[str, list[str]] = {}
    for name, value in rest:
        if name in ("press", "unit") or name.startswith(READ_PREFIX):
            continue
        if name.startswith(BOX_PREFIXES):
            prefix = next(p for p in BOX_PREFIXES if name.startswith(p))
            signs = f"correct:{name.removeprefix(prefix)}" if prefix == "refused:" else name
            if signs not in offered:
                raise PostRefused
            key, field = _box(name.removeprefix(prefix))
            if prefix == "none:":
                if field != "assigned" or value != "none" or key in none_given:
                    raise PostRefused
                none_given.add(key)
            elif prefix == "refused:":
                if field not in PIECE_DATES or (key, field) in carried:
                    raise PostRefused
                carried[(key, field)] = value
            else:
                if (key, field) in boxes:
                    raise PostRefused
                boxes[(key, field)] = value
            continue
        kind, _ = _field(name)
        if name not in offered or value not in offered[name]:
            raise PostRefused
        if name in chosen and kind != "instructions":
            raise PostRefused
        chosen.setdefault(name, []).append(value)
    folded = box_corrections(state, boxes, carried, none_given)
    corrections = folded.corrections
    answers = []
    for name, values in chosen.items():
        kind, subject = _field(name)
        if kind == "instructions":
            # Every value here matched an option the page signed, so each one but "none
            # apply" is an instruction's words as the page wrote them.
            texts = sorted({str(json.loads(v)) for v in values if v})
            if texts and "" in values:
                raise PostRefused
            value = json.dumps(texts)
        elif kind == "destination":
            value = _destination(values[0])
        else:
            value = values[0]
        answers.append(_answer(state, kind, subject, value, corrections))
    withdrawn = tuple(
        sorted(
            _field(name) for name in offered if name not in chosen and name.startswith(WITHDRAWABLE)
        )
    )
    return ReviewPost(
        presses[0],
        _number(hidden["version"]),
        hidden["acceptance"],
        _number(hidden["progress_revision"]),
        tuple(answers),
        corrections,
        units,
        digests,
        defaults,
        withdrawn,
        too_long=folded.too_long,
        needed=folded.needed,
        both=folded.both,
    )


BOX_PREFIXES: Final = ("correct:", "refused:", "none:")
"""The fields of the fold, by prefix: a box, a date Blossom couldn't read carried back beside
its box, and "No assigned date was given"."""
READ_PREFIX: Final = "read:"
"""The unsigned field beside each box that tells a box posted unchanged, read only when a
review that has gone gives its typing back (``typed_back``)."""


def _box(subject: str) -> tuple[str, str]:
    """A box's key and field, from its subject: a row key with ``course``, ``title`` or an
    instruction text's field, or a piece's dates key with ``due`` or ``assigned``."""
    try:
        key, field = (str(part) for part in json.loads(subject))
    except (ValueError, TypeError) as error:
        raise PostRefused from error
    if not correctable(field) or field in OLD_DATES or field == "instruction":
        raise PostRefused
    return key, field


class Folded(NamedTuple):
    """A post's boxes as corrections, and the boxes that refuse the press whole."""

    corrections: dict[str, dict[str, str | None]]
    too_long: tuple[tuple[str, str], ...]
    needed: tuple[tuple[str, str], ...]
    both: tuple[str, ...]


def box_corrections(
    state: ReviewState,
    boxes: Mapping[tuple[str, str], str],
    carried: Mapping[tuple[str, str], str],
    none_given: Iterable[str],
) -> Folded:
    """Each box kept as a correction exactly where it differs from its value with no
    correction, both stripped with line endings made line feeds and dates read as dates; the
    length check runs on corrections only. A cleared class, title or settled due date is
    refused; a cleared instruction text is its removal (``None``); a cleared assigned date is
    the date as read. "No assigned date was given" is none (``None``) where one was read, its
    box left empty or holding the date as read; beside any other date it is refused. A box the
    page signed for a card another press has since saved, or that a change of screenshots took
    away, is passed over, as the answers about it are."""
    reads = fold_reads(state)
    nones = set(none_given)
    out: dict[str, dict[str, str | None]] = {}
    too_long: set[tuple[str, str]] = set()
    needed: set[tuple[str, str]] = set()
    both: set[str] = set()

    def keep(key: str, field: str, value: str | None) -> None:
        out.setdefault(key, {})[field] = value

    nones = {key for key in nones if (key, "assigned") in reads}
    for (key, field), value in boxes.items():
        if (key, field) not in reads:
            continue
        read = reads[(key, field)]
        if field in PIECE_DATES:
            typed = value.strip() or carried.get((key, field), "").strip()
            none = field == "assigned" and key in nones
            day = typed_day(typed) if typed else None
            if none and typed and (day is None or day.isoformat() != read):
                both.add(key)
                keep(key, field, typed)
            elif none:
                if read is not None:
                    keep(key, field, None)
            elif not typed:
                if field == "due" and read is not None:
                    needed.add((key, field))
                    keep(key, field, "")
            elif len(typed) > field_most(field):
                too_long.add((key, field))
                keep(key, field, typed)
            elif day is None:
                keep(key, field, typed)
            elif day.isoformat() != read:
                keep(key, field, day.isoformat())
            continue
        text = correction_text(field, value)
        if canonical(text) == read:
            continue
        if not canonical(text):
            if instruction_index(field) is not None:
                keep(key, field, None)
            else:
                needed.add((key, field))
                keep(key, field, "")
        elif len(text) > field_most(field):
            too_long.add((key, field))
            keep(key, field, text)
        else:
            keep(key, field, canonical(text))
    for key in nones:
        if (key, "assigned") not in boxes and reads[(key, "assigned")] is not None:
            keep(key, "assigned", None)
    return Folded(out, tuple(sorted(too_long)), tuple(sorted(needed)), tuple(sorted(both)))


def typed_back(items: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Each correction a post typed, by field, in the order it was posted: a box's text that
    differs from its value with no correction as the ``read:`` field beside it names it, or for
    a date box posted empty, the date Blossom couldn't read that was carried beside it. Read
    from the post alone, never from the review, to give back as typed when the review can't
    take it; a name that doesn't name a correction is passed over."""
    found: dict[tuple[str, str], str] = {}
    carried: dict[tuple[str, str], str] = {}
    unchanged = {
        name.removeprefix(READ_PREFIX): value
        for name, value in items
        if name.startswith(READ_PREFIX)
    }
    for name, value in items:
        prefix = next((p for p in ("correct:", "refused:") if name.startswith(p)), None)
        if prefix is None:
            continue
        named = name.removeprefix(prefix)
        try:
            key, field = (str(part) for part in json.loads(named))
        except (ValueError, TypeError):
            continue
        if not correctable(field):
            continue
        text = correction_text(field, value)
        if prefix == "correct:":
            if unchanged.get(named) == read_digest(text):
                continue
            found.setdefault((key, field), text)
        else:
            carried.setdefault((key, field), text)
    for subject, text in carried.items():
        if not found.get(subject, "").strip():
            found[subject] = text
    return tuple((field, text) for (_, field), text in found.items() if text.strip())


# ------------------------------------------------------------------ saved progress

PROGRESS_KEYS: Final = frozenset({"v", "corrected", "selected"})
PROGRESS_SHAPE: Final = 4
"""The shape saved progress is written in: a row's corrections under its key, and each
piece's own dates under ``date:{piece name}``, a removal as null."""
OLD_SHAPE: Final = 3
"""Saved progress of shape 3: a row's corrections alone, its dates as its due line and day
heading. It reads with ``date_target``'s meaning until a press writes shape 4."""
OLD_FIELDS: Final = frozenset({"course", "title", "instruction", *OLD_DATES})
"""The fields saved progress of shape 3 holds."""


class SavedProgress(NamedTuple):
    """What Save review for later keeps: the typed corrections by key and field, and the row
    keys of the groups ticked, in the shape they were written in. Nothing else, and never an
    answer."""

    corrected: dict[str, dict[str, str | None]]
    selected: frozenset[str]
    shape: int = PROGRESS_SHAPE


def progress_text(corrected: Corrections, selected: Iterable[str]) -> str:
    """Saved progress in its one shape."""
    return json.dumps(
        {
            "v": PROGRESS_SHAPE,
            "corrected": {
                key: dict(sorted(fields.items())) for key, fields in sorted(corrected.items())
            },
            "selected": sorted(set(selected)),
        },
        separators=(",", ":"),
    )


def _kept(shape: int, key: str, field: str, value: object) -> bool:
    """Whether one entry of saved progress is one its shape holds: text within its field's
    limit that isn't blank; in the new shape, under a piece's dates key its due or assigned
    date, under a row key its class, title or an instruction text, a removal (null) only for
    an instruction text or an assigned date."""
    if shape == OLD_SHAPE:
        if field not in OLD_FIELDS:
            return False
    elif key.startswith("date:"):
        if field not in PIECE_DATES:
            return False
    elif field not in ROW_FIELDS and instruction_index(field) is None:
        return False
    if value is None:
        return shape != OLD_SHAPE and (field == "assigned" or instruction_index(field) is not None)
    return isinstance(value, str) and bool(value.strip()) and len(value) <= field_most(field)


def progress_read(text: str | None) -> SavedProgress | None:
    """Saved progress, read closed-world: exactly its three keys, shape 4 or shape 3,
    each entry one its shape holds (``_kept``), and row keys as text. Any other shape reads as
    no progress."""
    if text is None:
        return None
    try:
        found = json.loads(text)
    except ValueError:
        return None
    if not isinstance(found, dict) or set(found) != PROGRESS_KEYS:
        return None
    corrected, selected, shape = found["corrected"], found["selected"], found["v"]
    if shape not in (OLD_SHAPE, PROGRESS_SHAPE) or isinstance(shape, bool):
        return None
    if not isinstance(corrected, dict):
        return None
    if not isinstance(selected, list) or not all(isinstance(key, str) for key in selected):
        return None
    out: dict[str, dict[str, str | None]] = {}
    for key, fields in corrected.items():
        if not isinstance(fields, dict):
            return None
        if not all(_kept(shape, str(key), str(field), value) for field, value in fields.items()):
            return None
        out[str(key)] = {str(field): value for field, value in fields.items()}
    return SavedProgress(out, frozenset(selected), shape)


def shape_four(state: ReviewState, corrections: Corrections) -> dict[str, dict[str, str | None]]:
    """Corrections of saved progress of shape 3 as shape 4 holds them: each box as the page
    shows it, from the pieces those corrections give (``state``), kept where it differs from
    its value with no correction, as a first press posting the page untouched keeps it."""
    boxes: dict[tuple[str, str], str] = {}
    wordless = wordless_rows(state.open)
    for piece in state.open:
        read = piece.as_read
        if read is None:
            continue
        if (piece.key, "course") not in boxes:
            boxes[(piece.key, "course")] = piece.course
            boxes[(piece.key, "title")] = piece.title
            old = corrections.get(piece.key, {}).get("instruction")
            replaced = old is not None and usable_correction("instruction", old)
            for index, text in enumerate(box_texts(piece, wordless)):
                kept = (str(old).strip() if index == 0 else "") if replaced else text
                boxes[(piece.key, f"instruction:{index}")] = kept
        key = dates_key(piece.name)
        due = piece.due if piece.due_resolved else None
        boxes[(key, "due")] = "" if due is None else due.isoformat()
        boxes[(key, "assigned")] = "" if piece.assigned is None else piece.assigned.isoformat()
    return box_corrections(state, boxes, {}, ()).corrections
