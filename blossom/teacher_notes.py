# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A teacher's free-text notes read as tasks a family can review.

Under a school day the school lists, for a subject, a note in the teacher's own
words: one or more paragraphs. ``read_notes`` turns the notes of one paste into
tasks, each with the words it was read from, a standing (homework, not an
assignment, or unclear) and, only where a deadline statement settles one date,
a deadline.

The rules are closed lists, not a reading of language. A deadline statement is
"due" or "by" followed by one of a fixed set of date expressions, or "due" with
no word after it. Homework without a deadline statement is told by a fixed set
of opening words, each counted only when a fixed set of words after it does not
show it names a thing. Anything these lists do not settle is kept whole and marked
unclear: no split and no date is made up, and the school day a note is listed
under is never taken for a deadline.

Nothing here reads a clock or a store. "Today", "tomorrow" and a weekday are
worked out from the first school day a task is listed under, and a month and
day are read in that day's year unless a year is written with them, except that
January named under a school day in December is read in the next year. Four
digits after a date's comma or space that a counting noun follows, such as
"1500 words", count something and are no year.
"""

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Final

NAME_LIMIT: Final = 200
"""The most characters a task's name holds."""

_WEEK: Final = timedelta(days=7)


def _school_week(day: date) -> date:
    """The Monday that starts the school week a day is in. The same words listed again on a
    day of the first listing's school week are the same task; in a later week they are a
    task of their own, however few days apart."""
    return day - timedelta(days=day.weekday())


_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ENGLISH_MONTHS: Final = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_ENGLISH_SHORT: Final = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_SPANISH_MONTHS: Final = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_SPANISH_SHORT: Final = {
    "ene": 1,
    "feb": 2,
    "mar": 3,
    "abr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dic": 12,
}
_ENGLISH: Final = {name: number for number, name in enumerate(_ENGLISH_MONTHS, 1)} | _ENGLISH_SHORT
_SPANISH: Final = {name: number for number, name in enumerate(_SPANISH_MONTHS, 1)} | _SPANISH_SHORT

_ABBREVIATIONS: Final = frozenset(
    ({*_ENGLISH_SHORT, *_SPANISH_SHORT} - {"may", "ago"})
    | {"mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri"}
    | {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "approx", "min", "hr", "hrs", "wk"}
    | {"p", "pp", "pg", "pgs", "ch", "chap", "sec", "fig", "vol"}
)
"""Words whose period may not end a sentence, so a split after one is unsure."""


def _months(names: Sequence[str], short: Sequence[str]) -> str:
    """Month names as a pattern: a whole name, or an abbreviation with its optional period."""
    return rf"(?:(?:{'|'.join(names)})\b|(?:{'|'.join(sorted(short, key=len, reverse=True))})\b\.?)"


_RELATIVE_DAYS: Final = {"today": 0, "tomorrow": 1}
_RELATIVE: Final = re.compile(r"(today|tomorrow)\b", re.IGNORECASE)
_WEEKDAY: Final = re.compile(
    rf"(?:(?P<next>next)\s+)?(?P<weekday>{'|'.join(_WEEKDAYS)})\b", re.IGNORECASE
)
_BESIDE: Final = re.compile(r"\s*,?\s*")
_COUNTING_NOUNS: Final = (
    "words",
    "word",
    "pages",
    "page",
    "points",
    "minutes",
    "questions",
    "problems",
    "sentences",
    "palabras",
    "palabra",
    "p\u00e1ginas",
    "puntos",
    "minutos",
    "preguntas",
    "problemas",
    "oraciones",
)
"""The nouns that, right after four digits, make them a count and not a year."""
_COUNTED: Final = rf"(?![^\S\n]+(?:{'|'.join(_COUNTING_NOUNS)})\b)"
"""After four digits joined to a date by a comma or a space: no counting noun follows, so
the digits are a year and not a count ("January 8, 1500 words")."""
_EXPLICIT: Final = re.compile(
    rf"(?<![A-Za-z])(?P<english>{_months(_ENGLISH_MONTHS, tuple(_ENGLISH_SHORT))})"
    r"\s+(?P<english_day>\d{1,2})(?:st|nd|rd|th)?\b"
    rf"(?:,?\s+(?P<english_year>\d{{4}})\b{_COUNTED})?"
    r"|(?<![\d/])(?P<numeric_month>\d{1,2})/(?P<numeric_day>\d{1,2})"
    r"(?:/(?P<numeric_year>\d{4}))?(?![\d/])"
    rf"|(?<!\d)(?P<spanish_day>\d{{1,2}})\s+de\s+"
    rf"(?P<spanish>{_months(_SPANISH_MONTHS, tuple(_SPANISH_SHORT))})"
    r"(?:\s+del?\s+(?P<spanish_year>\d{4})\b"
    rf"|,\s*(?P<spanish_listed_year>\d{{4}})\b{_COUNTED})?",
    re.IGNORECASE,
)
_CUE: Final = re.compile(r"\b(?:due|by)\b", re.IGNORECASE)
_GAP: Final = re.compile(r"\s+")
_DUE_ON: Final = re.compile(r"\s+(?:on\s+)?", re.IGNORECASE)
_DUE_BY: Final = re.compile(r"\s+by\s+", re.IGNORECASE)
_NOTHING_AFTER: Final = re.compile(r"[^\S\n]*(?:\n|[^\w\s]|\Z)")
_WORD: Final = re.compile(r"\w")
_WORD_START: Final = re.compile(r"(?<![\w/])\w")
_END: Final = re.compile(r"(?P<run>[.!?]+)[\"')\]]*(?P<gap>\s+)(?=\S)")
_LAST_WORD: Final = re.compile(r"[A-Za-z]+$")
_OPENING_WORDS: Final = r"(?:please\s+bring|go\s+to|study|use|check|bring|review)\b(?![-'])"
_OPENING: Final = re.compile(rf"\W*{_OPENING_WORDS}", re.IGNORECASE)
_OPENING_INSIDE: Final = re.compile(rf"(?<![\w'-]){_OPENING_WORDS}", re.IGNORECASE)
_NEXT_WORD: Final = re.compile(r"[\w']+")
_LINKING: Final = frozenset({"is", "are", "was", "were", "has", "have", "had", "went", "will"})
"""Words that, among the three after an opening word, show it names a thing, not a task."""
_NO_HOMEWORK: Final = re.compile(r"\bno\b(?:\s+[\w']+){0,3}\s+homework\b", re.IGNORECASE)
_IS_ARE: Final = re.compile(r"(?:^|\s+)(?:is|are)$", re.IGNORECASE)
_TRAILING: Final = " ,;:-"


class Standing(enum.StrEnum):
    """What a task read from a note looks like."""

    HOMEWORK = "homework"
    UNCLEAR = "unclear"
    NOT_ASSIGNMENT = "not_assignment"


@dataclass(frozen=True)
class NoteBlock:
    """One teacher's note as read: a subject, the school day it is listed under, its text.

    Each paragraph is kept as written, its lines joined by a line break. ``line``
    is the line of the note's heading in the paste, or 0. ``suspect`` says the
    reader saw a line in the note that it could not vouch for.
    """

    subject: str
    day: date
    paragraphs: tuple[str, ...]
    line: int
    suspect: bool = False


@dataclass(frozen=True)
class NoteTask:
    """One task read from the notes of a paste.

    ``name`` is a candidate title and ``words`` the task's source words with
    its supporting text, as written, paragraphs joined by a blank line.
    ``first_day`` is the first school day it is listed under and ``seen_on``
    every such day, ascending. ``deadline`` is set only when one deadline
    statement settles one date; ``deadline_words`` is that statement as
    written. ``choices`` holds the dates an unsettled statement could mean, and
    ``named`` the dates the words name outside a deadline statement, which are
    never the deadline. ``listed_after`` holds the school days the task is
    still listed under after its deadline. ``line`` is the line of the note it
    was first listed under.
    """

    subject: str
    name: str
    words: str
    standing: Standing
    first_day: date
    seen_on: tuple[date, ...]
    deadline: date | None
    deadline_words: str | None
    choices: tuple[date, ...]
    named: tuple[date, ...]
    listed_after: tuple[date, ...]
    line: int


@dataclass(frozen=True)
class _Statement:
    """A deadline statement in a piece of text, with every date it could mean.

    No date means "due" alone; one date means the statement settles it.
    """

    start: int
    end: int
    words: str
    dates: tuple[date, ...]


@dataclass(frozen=True)
class _Unit:
    """A paragraph, or one sentence of a paragraph that was split, as read."""

    paragraph: int
    start: int
    end: int
    text: str
    statements: tuple[_Statement, ...]
    named: tuple[date, ...]
    unsure: bool = False

    @property
    def before(self) -> str:
        """The words before the first deadline statement."""
        return self.text[: self.statements[0].start] if self.statements else self.text

    @property
    def only_statement(self) -> bool:
        """Whether the text is one deadline statement and nothing else."""
        if len(self.statements) != 1:
            return False
        only = self.statements[0]
        return not _WORD.search(self.text[: only.start] + self.text[only.end :])


@dataclass(frozen=True)
class _Reading:
    """A task as one note gives it, before it is set beside the other days."""

    name: str
    words: str
    standing: Standing
    deadline: date | None
    deadline_words: str | None
    choices: tuple[date, ...]
    named: tuple[date, ...]


@dataclass
class _Seen:
    """A task with every day and place it has been listed so far."""

    reading: _Reading
    block: NoteBlock
    days: set[date]
    where: tuple[int, int]


def _single(text: str) -> str:
    """The words single-spaced."""
    return " ".join(text.split())


def _explicit_at(text: str, at: int, day: date) -> tuple[int, date] | None:
    """The month and day written at a place and where it ends, in the year written with it,
    or with none, in the school day's year, except a January date read under a school day in
    December, which is in the following year. Four digits joined to the date by a comma or a
    space with a counting noun right after them count something ("January 8, 1500 words")
    and are no year; with any other words after them, after "de" or "del", or in the numeric
    form, they are the year. A date not on the calendar is none."""
    found = _EXPLICIT.match(text, at)
    if found is None:
        return None
    if found["english"]:
        month, number = _ENGLISH[found["english"].lower().rstrip(".")], found["english_day"]
    elif found["spanish"]:
        month, number = _SPANISH[found["spanish"].lower().rstrip(".")], found["spanish_day"]
    else:
        month, number = int(found["numeric_month"]), found["numeric_day"]
    written = (
        found["english_year"]
        or found["numeric_year"]
        or found["spanish_year"]
        or found["spanish_listed_year"]
    )
    if written:
        year = int(written)
    else:
        year = day.year + 1 if (day.month, month) == (12, 1) else day.year
    try:
        return found.end(), date(year, month, int(number))
    except ValueError:
        return None


def _date_at(text: str, at: int, day: date) -> tuple[int, tuple[date, ...]] | None:
    """The date expression written at a place: where it ends and every date it could mean.

    One date means the expression settles it. A weekday the school day shares,
    "next" before a weekday, and a weekday beside a date that falls on another
    weekday each give more than one.
    """
    relative = _RELATIVE.match(text, at)
    if relative is not None:
        return relative.end(), (day + timedelta(days=_RELATIVE_DAYS[relative[1].lower()]),)
    weekday = _WEEKDAY.match(text, at)
    if weekday is None:
        explicit = _explicit_at(text, at, day)
        return None if explicit is None else (explicit[0], (explicit[1],))
    number = _WEEKDAYS.index(weekday["weekday"].lower())
    ahead = (number - day.weekday()) % 7
    could: tuple[date, ...]
    if weekday["next"]:
        following = day + timedelta(days=ahead or 7)
        could = (following, following + _WEEK)
    elif ahead == 0:
        could = (day, day + _WEEK)
    else:
        could = (day + timedelta(days=ahead),)
    beside = _BESIDE.match(text, weekday.end())
    explicit = _explicit_at(text, beside.end() if beside else weekday.end(), day)
    if explicit is None:
        return weekday.end(), could
    end, exact = explicit
    if exact.weekday() == number:
        return end, (exact,)
    return end, tuple(sorted({exact, *could}))


def _statements(text: str, day: date) -> tuple[_Statement, ...]:
    """Every deadline statement in the text, in order.

    "due" or "by" followed by a date expression, "due on" or "due by" followed
    by one, or "due" where the end of the text, a line end or punctuation comes
    next. "by" with no date expression after it is not one, and neither is
    "due" before any other word.
    """
    found: list[_Statement] = []
    covered = 0
    for cue in _CUE.finditer(text):
        if cue.start() < covered:
            continue
        is_due = cue[0].lower() == "due"
        end = cue.end()
        dates: tuple[date, ...] = ()
        for lead in (_DUE_ON, _DUE_BY) if is_due else (_GAP,):
            gap = lead.match(text, cue.end())
            expression = _date_at(text, gap.end(), day) if gap else None
            if expression is not None:
                end, dates = expression
                break
        else:
            if not is_due or _NOTHING_AFTER.match(text, cue.end()) is None:
                continue
        found.append(_Statement(cue.start(), end, text[cue.start() : end], dates))
        covered = end
    return tuple(found)


def _named(text: str, statements: Sequence[_Statement], day: date) -> tuple[date, ...]:
    """The dates the text names outside its deadline statements, where each names one date."""
    dates: list[date] = []
    resume = 0
    for word in _WORD_START.finditer(text):
        at = word.start()
        if at < resume:
            continue
        inside = next((each for each in statements if each.start <= at < each.end), None)
        if inside is not None:
            resume = inside.end
            continue
        expression = _date_at(text, at, day)
        if expression is None:
            continue
        resume, could = expression
        if len(could) == 1:
            dates.append(could[0])
    return tuple(dates)


def _unsure(text: str, start: int, end: re.Match[str]) -> bool:
    """Whether a sentence end found in the text may be something else.

    ``start`` is where the sentence began. The end is unsure when the next
    words do not open with a capital letter, when it is an ellipsis, and when
    its period follows an abbreviation, a single letter or a bare number.
    """
    if not text[end.end()].isupper():
        return True
    if end["run"] != ".":
        return "." in end["run"]
    sentence = text[start : end.start()]
    last = _LAST_WORD.search(sentence)
    if last is not None and (len(last[0]) == 1 or last[0].lower() in _ABBREVIATIONS):
        return True
    return sentence.strip().isdigit()


def _sentences(text: str, statements: Sequence[_Statement]) -> list[tuple[int, int]] | None:
    """Where each sentence of a paragraph starts and ends, or None if any split is unsure."""
    spans: list[tuple[int, int]] = []
    start = 0
    for end in _END.finditer(text):
        if any(each.start < end.start() and end.end() <= each.end for each in statements):
            continue
        if _unsure(text, start, end):
            return None
        spans.append((start, end.start("gap")))
        start = end.end()
    spans.append((start, len(text)))
    return spans


def _cut(name: str) -> str:
    """The name at no more than the limit, cut between words where it has any."""
    if len(name) <= NAME_LIMIT:
        return name
    kept = name[:NAME_LIMIT]
    if not name[NAME_LIMIT].isspace() and kept.rfind(" ") > 0:
        kept = kept[: kept.rfind(" ")]
    return kept.rstrip()


def _name_before(before: str) -> str:
    """A name from the words before a deadline statement, without a trailing "is" or "are"."""
    name = _single(before).rstrip(_TRAILING)
    return _cut(_IS_ARE.sub("", name).rstrip(_TRAILING))


def _headline(text: str) -> str:
    """A name from the first sentence or line of the text."""
    line = next((each for each in text.splitlines() if each.strip()), text)
    for end in _END.finditer(line):
        if not _unsure(line, 0, end):
            line = line[: end.start()]
            break
    return _cut(_single(line).rstrip(".!?")) or _cut(_single(text))


def _asks(words: str, found: re.Match[str]) -> bool:
    """Whether an opening word found in the words asks for a task: the word after it is not
    "of", and none of the next three words is "is", "are", "was", "were", "has", "have",
    "had", "went" or "will"."""
    after = [word.lower() for word in _NEXT_WORD.findall(words, found.end())[:3]]
    return after[:1] != ["of"] and _LINKING.isdisjoint(after)


def _opens(words: str) -> bool:
    """Whether the words open with an opening word that asks for a task."""
    found = _OPENING.match(words)
    return found is not None and _asks(words, found)


def _sets_work(words: str) -> bool:
    """Whether an opening word that asks for a task starts any word of the words."""
    return any(_asks(words, found) for found in _OPENING_INSIDE.finditer(words))


def _unit(paragraph: int, text: str, start: int, end: int, day: date) -> _Unit:
    """One piece of a paragraph with its deadline statements and the dates it names."""
    piece = text[start:end]
    statements = _statements(piece, day)
    return _Unit(paragraph, start, end, piece, statements, _named(piece, statements, day))


def _units(paragraph: int, text: str, day: date) -> list[_Unit]:
    """A paragraph as the pieces that can each be a task.

    It is split into sentences only when it carries a deadline statement and
    every sentence end in it is sure. Otherwise it stays whole, and a paragraph
    kept whole because a split was unsure says so.
    """
    whole = _unit(paragraph, text, 0, len(text), day)
    if not whole.statements or whole.only_statement:
        return [whole]
    spans = _sentences(text, whole.statements)
    if spans is None:
        return [replace(whole, unsure=True)]
    if len(spans) == 1:
        return [whole]
    return [_unit(paragraph, text, start, end, day) for start, end in spans]


def _words(units: Sequence[_Unit], paragraphs: Sequence[str]) -> str:
    """The words of a task as written: each paragraph's part, joined by a blank line."""
    parts: list[str] = []
    for paragraph in sorted({unit.paragraph for unit in units}):
        mine = [unit for unit in units if unit.paragraph == paragraph]
        parts.append(paragraphs[paragraph][mine[0].start : mine[-1].end])
    return "\n\n".join(parts)


def _reading(units: Sequence[_Unit], paragraphs: Sequence[str]) -> _Reading:
    """One task from the pieces that make it up.

    Words that say no homework is assigned are not an assignment unless they also set work:
    an opening word that asks for a task anywhere in them, a deadline statement after more
    words than the ones that say no homework, or a later deadline statement after the one
    that says no homework. Words that set work as well are unclear, and the date each later
    deadline statement settles is named for review.
    """
    first = units[0]
    words = _words(units, paragraphs)
    statements = [statement for unit in units for statement in unit.statements]
    before = _name_before(first.before) if first.statements else ""
    name = before or _headline(first.text)
    says_none = bool(_NO_HOMEWORK.search(name if first.statements else words))
    opens = _opens(words)
    unsure = any(unit.unsure for unit in units)
    named = {day for unit in units for day in unit.named}
    if not statements:
        if says_none:
            standing = Standing.UNCLEAR if _sets_work(words) else Standing.NOT_ASSIGNMENT
        else:
            standing = Standing.HOMEWORK if opens else Standing.UNCLEAR
    elif says_none:
        alone = _NO_HOMEWORK.fullmatch(name) is not None and len(statements) == 1
        standing = Standing.NOT_ASSIGNMENT if alone and not _sets_work(words) else Standing.UNCLEAR
        named |= {statement.dates[0] for statement in statements[1:] if len(statement.dates) == 1}
    elif len(statements) > 1 or unsure or (first.statements and not before):
        standing = Standing.UNCLEAR
    else:
        standing = Standing.HOMEWORK
    dates = statements[0].dates if len(statements) == 1 else ()
    return _Reading(
        name=name,
        words=words,
        standing=standing,
        deadline=dates[0] if len(dates) == 1 else None,
        deadline_words=statements[0].words if len(statements) == 1 else None,
        choices=dates if len(dates) > 1 else (),
        named=tuple(sorted(named)),
    )


def _read_block(block: NoteBlock) -> list[_Reading]:
    """The tasks of one note, in the order it writes them.

    A piece with a deadline statement of its own starts a task. A piece with
    none, or one that is only a deadline statement, joins the task before it;
    at the head of a note it is the first task. A suspect note is one unclear
    task holding all of it, with no date read from it.
    """
    paragraphs = [paragraph.strip() for paragraph in block.paragraphs if paragraph.strip()]
    if not paragraphs:
        return []
    if block.suspect:
        words = "\n\n".join(paragraphs)
        return [_Reading(_headline(paragraphs[0]), words, Standing.UNCLEAR, None, None, (), ())]
    tasks: list[list[_Unit]] = []
    for paragraph, text in enumerate(paragraphs):
        for unit in _units(paragraph, text, block.day):
            if tasks and (not unit.statements or unit.only_statement):
                tasks[-1].append(unit)
            else:
                tasks.append([unit])
    return [_reading(units, paragraphs) for units in tasks]


def read_notes(blocks: Sequence[NoteBlock]) -> tuple[NoteTask, ...]:
    """The tasks of a whole paste, in order of first appearance.

    The same subject and the same task words, compared single-spaced, listed
    again on a day of the school week, Monday to Sunday, of the task's first
    school day are one task: the day is added to ``seen_on`` and nothing else
    about the task changes, so its deadline stays as read from the first day.
    The same words listed in a later school week are a task of its own,
    however few days apart, and so is anything else, whatever its name.
    """
    latest: dict[tuple[str, str, bool], _Seen] = {}
    found: list[_Seen] = []
    by_day = sorted(enumerate(blocks), key=lambda placed: (placed[1].day, placed[0]))
    for position, block in by_day:
        for place, reading in enumerate(_read_block(block)):
            key = (_single(block.subject), _single(reading.words), block.suspect)
            seen = latest.get(key)
            if seen is not None and _school_week(block.day) == _school_week(seen.block.day):
                seen.days.add(block.day)
                seen.where = min(seen.where, (position, place))
                continue
            latest[key] = _Seen(reading, block, {block.day}, (position, place))
            found.append(latest[key])
    return tuple(_task(seen) for seen in sorted(found, key=lambda seen: seen.where))


def _task(seen: _Seen) -> NoteTask:
    """The task as handed back, with its days in order."""
    reading, days = seen.reading, tuple(sorted(seen.days))
    deadline = reading.deadline
    return NoteTask(
        subject=seen.block.subject,
        name=reading.name,
        words=reading.words,
        standing=reading.standing,
        first_day=seen.block.day,
        seen_on=days,
        deadline=deadline,
        deadline_words=reading.deadline_words,
        choices=reading.choices,
        named=reading.named,
        listed_after=tuple(day for day in days if deadline is not None and day > deadline),
        line=seen.block.line,
    )
