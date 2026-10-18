# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Reading a screenshot review's posts against what their page signed.

The review form carries its answers, typed corrections and ticked groups, and signs its hidden
fields, among them every question it asked with the values it offered. An offer, or a row
found under another name, is a whole change of its own: a form whose every field is signed.
A post that doesn't match what its page signed is refused whole, so nothing of it is kept.
"""

import dataclasses
import hmac
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Final, NamedTuple

from blossom.screenshot_page import page_check
from blossom.screenshot_review import DATE_FIELDS, answer_names, dest_kind, value_json, value_of
from blossom.screenshot_save import CORRECTABLE, ReviewState, correction_digest
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
MOST_CORRECTION: Final = 200
"""The most characters of one typed correction."""


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
    corrections: dict[str, dict[str, str]]
    units: tuple[str, ...]
    digests: dict[str, tuple[str, ...]]
    defaults: dict[str, str]
    withdrawn: tuple[tuple[str, str], ...] = ()
    """Each confirmation, instruction choice or "These are her homework" the page asked for and
    the press left blank, by kind and subject: a box left unticked takes back the answer it
    held."""
    ticked: bool = True
    """Whether the press posted the page's ticks; an offer or a found row posts none."""


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
    corrections: Mapping[str, Mapping[str, str]],
) -> AnswerToKeep:
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
    corrections: Mapping[str, Mapping[str, str]],
) -> ReviewPost:
    """An offer's or a found row's post: every field signed, destination answers alone, kept
    as a press of Keep these answers, with the corrections they were given with."""
    fields = _single(items)
    signature = fields.pop("offer", "")
    if not hmac.compare_digest(signature, page_check(page_key, sorted(fields.items()))):
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
    value it offered, each correction one for a field it asked to confirm, each ticked group
    one it showed. A confirmation or instruction question the page asked and the press left
    blank is withdrawn. A date Blossom couldn't read, carried back beside its empty date box,
    stays the box's correction until a date is chosen there."""
    signed = [(n, v) for n, v in items if n in SIGNED or n.startswith("digest:") or n == "check"]
    hidden = _single(signed)
    check = hidden.pop("check", "")
    if not hmac.compare_digest(check, page_check(page_key, list(hidden.items()))):
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
    corrections: dict[str, dict[str, str]] = {}
    carried: dict[tuple[str, str], str] = {}
    chosen: dict[str, list[str]] = {}
    for name, value in rest:
        if name in ("press", "unit"):
            continue
        if name.startswith("refused:"):
            subject = name.removeprefix("refused:")
            if f"answer:confirm:{subject}" not in offered or len(value) > MOST_CORRECTION:
                raise PostRefused
            try:
                key, field = (str(part) for part in json.loads(subject))
            except (ValueError, TypeError) as error:
                raise PostRefused from error
            if field not in DATE_FIELDS or (key, field) in carried:
                raise PostRefused
            carried[(key, field)] = value.strip()
            continue
        if name.startswith("correct:"):
            subject = name.removeprefix("correct:")
            if f"answer:confirm:{subject}" not in offered or len(value) > MOST_CORRECTION:
                raise PostRefused
            try:
                key, field = (str(part) for part in json.loads(subject))
            except (ValueError, TypeError) as error:
                raise PostRefused from error
            if field not in CORRECTABLE:
                raise PostRefused
            if value.strip():
                corrections.setdefault(key, {})[field] = value.strip()
            continue
        kind, _ = _field(name)
        if name not in offered or value not in offered[name]:
            raise PostRefused
        if name in chosen and kind != "instructions":
            raise PostRefused
        chosen.setdefault(name, []).append(value)
    for (key, field), text in carried.items():
        if text and not corrections.get(key, {}).get(field):
            corrections.setdefault(key, {})[field] = text
    answers = []
    for name, values in chosen.items():
        kind, subject = _field(name)
        if kind == "instructions":
            texts = sorted({v for v in values if v})
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
    )


# ------------------------------------------------------------------ saved progress

PROGRESS_KEYS: Final = frozenset({"v", "corrected", "selected"})
PROGRESS_SHAPE: Final = 3


class SavedProgress(NamedTuple):
    """What Save review for later keeps: the typed corrections by row key and field, and the
    row keys of the groups ticked. Nothing else, and never an answer."""

    corrected: dict[str, dict[str, str]]
    selected: frozenset[str]


def progress_text(corrected: Mapping[str, Mapping[str, str]], selected: Iterable[str]) -> str:
    """Saved progress in its one shape."""
    return json.dumps(
        {
            "v": PROGRESS_SHAPE,
            "corrected": {key: dict(fields) for key, fields in sorted(corrected.items())},
            "selected": sorted(set(selected)),
        },
        separators=(",", ":"),
    )


def progress_read(text: str | None) -> SavedProgress | None:
    """Saved progress, read closed-world: exactly its three keys, shape 3, corrections only to
    fields a page asks to confirm and within the form's limit, and row keys as text. Any other
    shape reads as no progress."""
    if text is None:
        return None
    try:
        found = json.loads(text)
    except ValueError:
        return None
    if not isinstance(found, dict) or set(found) != PROGRESS_KEYS:
        return None
    corrected, selected = found["corrected"], found["selected"]
    if found["v"] != PROGRESS_SHAPE or not isinstance(corrected, dict):
        return None
    if not isinstance(selected, list) or not all(isinstance(key, str) for key in selected):
        return None
    out: dict[str, dict[str, str]] = {}
    for key, fields in corrected.items():
        if not isinstance(fields, dict):
            return None
        for field, value in fields.items():
            if field not in CORRECTABLE or not isinstance(value, str):
                return None
            if not value.strip() or len(value) > MOST_CORRECTION:
                return None
        out[str(key)] = {str(field): str(value) for field, value in fields.items()}
    return SavedProgress(out, frozenset(selected))
