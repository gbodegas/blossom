# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A teacher's free-text notes read as tasks a family can review.

Under a school day the school lists, for a subject, a note in the teacher's own
words: one or more paragraphs. ``read_notes`` turns the notes of one paste into
tasks, each with the words it was read from, a standing (homework, not an
assignment, or unclear) and, only where the note establishes one current date,
a deadline.

The rules are closed lists, not a reading of language. A deadline statement is
"due" or "by" followed by one of a fixed set of date expressions, or "due" with
no word after it. Date expressions listed after a cue and joined by a final "or"
are one statement that could mean any of them. A fragment, a day number or a
weekday name or short form joined to a statement's date by "or", "and", "&",
"to", "through", "thru", a slash, a hyphen or dash, or a comma, is one more date
it could mean; a day number takes the joined date's month and year, and a
number a counting noun follows is no fragment.

A clause mark is ", ; : ( ) [ ] !", a hyphen or dash followed by a space, or a
line end; sure sentence ends bound every clause, and an unsure period is no
clause mark. A statement is negated when a negation word (not, never, no,
nothing, none, neither, "no" then "longer", a word ending in "n't" with an ASCII
or typographic apostrophe, or isnt, arent, wasnt, werent, dont, doesnt, didnt,
wont, cant, cannot) stands right before its cue or right before linking words (is, are,
was, were, be, been, will) right before it, or when no, nothing, none or neither
opens its clause with at most two words and linking words before the cue. It is
superseded when one of the supersession words (_SUPERSESSION_WORDS, "had been",
or "used" then "to") stands the same way, or when a change word (moved, changed,
postponed, rescheduled, extended, delayed, canceled in either spelling, pushed)
follows its date in its own clause. A
negated or superseded statement is never a deadline, offers no date, labels no
milestone and counts as no statement for splitting. A statement whose sentence
says no homework before its cue keeps the cue-only negation: not, never, "no"
then "longer" or a word ending in "n't" right before it, with at most "be"
between; a task that says no homework is read by the no-homework rule alone.

A task's deadline is a date only when the task is one sentence, counted by sure
sentence ends, with exactly one statement neither negated nor superseded, that
statement means one date, and the sentence is, in order, only: an optional
lead-in, which is a reminder opening ("don't forget", "do not forget",
"remember" or "reminder", with or without "that" and ":") and nothing else; a
lead holding no negation, supersession, hedge or conditional word, no change
word used as a verb about a date, no modal or semi-modal verb other than "will"
(can, could, must, shall, should, would, or ought, supposed, expected, going,
have, has, had, need or needs before "to"), no subject pronoun (I, we, you, he,
she, they) followed by a word that is not a linking word, no sentence adverb,
no word ending in "ly" right before the cue or before linking words right
before it other than the lead's first word ("Reply due Friday." sets its date;
"Reading log is usually due Friday." sets none), and at most one clause mark,
right before the cue, so any other opening clause ("Tentative:", "Extra
credit:") leaves the deadline unset; the cue and the date; an optional clock
time ("at" or "by" and a
time of day, after a space) or count (a number and a counting noun, after a
space, a comma or a line end); an optional control clause, ", not" and date
expressions; and the sentence end, which is not "?". After a negated or
superseded statement in the sentence the lead starts past it and the one clause
mark right after it. The control clause's dates are neither offered nor named.
A sentence adverb is a word ending in "ly" followed by a determiner (the, a, an,
your, our, this, that, these, those), wherever it stands in the lead, the
lead's first word included ("Hopefully the essay is due Friday." sets none);
the one exception is a verb that opens a direct instruction (multiply, apply,
supply, reply or comply) standing as the lead's first word ("Multiply the
fractions by Friday." sets its date).

Any other task has no deadline and offers its dates unselected: as choices, the
dates of its statements that are neither negated nor superseded and their
fragments; as named dates, every date any other date expression in its words
could mean outside every statement, or, in a task holding no statement, only
the dates that one expression settles. With two or more such statements, each
statement's dates are kept with its label, its clause's words before its cue
without joining words, a leading clock time or a trailing "is" or "are", when
every label is one to six words holding no "due", "by" or "or" and no negation,
supersession or hedge word, and no statement of the task was joined to it as a
correction; otherwise there are no labels. A label with several dates keeps one
milestone per date. A task whose first statement is negated or superseded takes
as its title that statement's clause words before its cue, less trailing
linking, negation and supersession words, when one to eight words remain and
none is a negation word.

Within one note, a piece whose first such statement names no separate
assignment joins the task before it: its lead, after any lead-in, holds only
"now", the pronouns it, this, that or they (with "'s" or "'re") and linking
words; or a pronoun (it, this, that, they, these or those, with "'s" or "'re"
or not) stands right before its cue, or right before linking words and "now"
right before it, whatever words come before the pronoun; or "now" stands
right before its cue or before linking words right
before it, and nothing is left of its lead once the lead-in, the words up to
its last clause mark, determiners, linking words, "now", words ending in "ly"
and the preceding task's title words are removed. A piece does not join by its
pronoun or "now" when the words of its lead before the last clause mark ahead
of them are no remark, are two or more and share no word with the preceding
task's title: they name an assignment of its own, the piece starts a task
titled by those words, and its date is read from its own wording. Such words
are a remark, and name nothing, when they open with "please" or hold note,
update, correction, reminder or sorry ("Please note:", "Quick update:"). A
statement whose sentence
says no homework never joins. Pieces with no statement before a note's first
piece holding one join that piece's task when they name no date, read on their
own as unclear (neither homework nor a note that says no homework), and that
piece does not say no homework; the title and standing are read from that
piece. Homework without a deadline
statement is told by a fixed set of opening words, each counted only when a
fixed set of words after it does not show it names a thing. Anything these
lists do not settle is kept whole and marked unclear: no split and no date is
made up, and the school day a note is listed under is never taken for a
deadline.

Nothing here reads a clock or a store. "Today", "tomorrow" and a weekday are
worked out from the first school day a task is listed under, and a month and
day are read in that day's year unless a year is written with them, except that
January named under a school day in December is read in the next year. Four
digits after a date's comma or space that a counting noun follows, on the same
line or the next, such as "1500 words", count something and are no year.
"""

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import pairwise
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
_COUNTED: Final = rf"(?!\s+(?:{'|'.join(_COUNTING_NOUNS)})\b)"
"""After four digits joined to a date by a comma or a space: no counting noun follows, on
the same line or after a line break, so the digits are a year and not a count ("January 8,
1500 words")."""
_EXPLICIT: Final = re.compile(
    rf"(?<![A-Za-z])(?P<english>{_months(_ENGLISH_MONTHS, tuple(_ENGLISH_SHORT))})"
    r"\s+(?P<english_day>\d{1,2})(?:st|nd|rd|th)?\b"
    rf"(?:(?:,\s*|\s+)(?P<english_year>\d{{4}})\b{_COUNTED})?"
    r"|(?<![\d/])(?P<numeric_month>\d{1,2})/(?P<numeric_day>\d{1,2})"
    r"(?:/(?P<numeric_year>\d{4}))?(?![\d/])"
    rf"|(?<!\d)(?P<spanish_day>\d{{1,2}})\s+de\s+"
    rf"(?P<spanish>{_months(_SPANISH_MONTHS, tuple(_SPANISH_SHORT))})"
    r"(?:\s+del?\s+(?P<spanish_year>\d{4})\b"
    rf"|,\s*(?P<spanish_listed_year>\d{{4}})\b{_COUNTED})?",
    re.IGNORECASE,
)
_CUE: Final = re.compile(r"\b(?:due|by)\b", re.IGNORECASE)
_NEGATED: Final = re.compile(
    r"(?:\b(?:not|never|no\s+longer)|\w+n['\N{RIGHT SINGLE QUOTATION MARK}]t)"
    r"\s+(?:be\s+)?\Z",
    re.IGNORECASE,
)
"""The words right before a cue that negate it: "not", "never", "no" then "longer", or a word
ending in "n't", its apostrophe ASCII or typographic (U+2019), with at most "be" between."""
_OR: Final = re.compile(r"\s*,?\s+or\s+", re.IGNORECASE)
_LISTED: Final = re.compile(r"\s*,\s*")
"""The comma between two date expressions of a list."""
_JOINING: Final = re.compile(r"(?:\W|\b(?:and|then)\b)*", re.IGNORECASE)
"""Punctuation, spaces and the joining words "and" and "then" that open a clause."""
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
_APOSTROPHES: Final = "'\N{RIGHT SINGLE QUOTATION MARK}"
_LINK_WORDS: Final = frozenset({"is", "are", "was", "were", "be", "been", "will"})
_LINK: Final = rf"(?:{'|'.join(sorted(_LINK_WORDS))})"
_NEGATION_WORDS: Final = frozenset(
    {"not", "never", "no", "nothing", "none", "neither"}
    | {"isnt", "arent", "wasnt", "werent", "dont", "doesnt", "didnt", "wont", "cant", "cannot"}
)
_SUPERSESSION_WORDS: Final = frozenset({"was", "were", "originally", "previous" + "ly"})
_SUPERSESSION: Final = re.compile(
    r"\b(?:was|were|originally|previous(?:ly)|had\s+been|used\s+to)\b", re.IGNORECASE
)
_HEDGE: Final = re.compile(
    r"\b(?:maybe|might|perhaps|probably|possibly|tentatively|likely|should\s+be)\b"
    r"|\bmay\b(?!\s+\d)",
    re.IGNORECASE,
)
_CONDITIONAL: Final = re.compile(r"\b(?:if|unless|except|only)\b", re.IGNORECASE)
_CHANGE_WORDS: Final = (
    r"(?:moved|changed|postponed|rescheduled|extended|delayed|cancel{1,2}ed|pushed)"
)
_CHANGE: Final = re.compile(rf"\b{_CHANGE_WORDS}\b", re.IGNORECASE)
_CHANGE_VERB: Final = re.compile(
    rf"\b{_CHANGE_WORDS}\s+(?:to|until|till|back|from)\b"
    rf"|\b(?:is|are|was|were|been|has|have)\s+{_CHANGE_WORDS}\b",
    re.IGNORECASE,
)
"""A change word used as a verb about a date: followed by to, until, till, back or from, or
after is, are, was, were, been, has or have ("Extended response" holds none)."""
_MARK: Final = re.compile(r"[,;:()\[\]!\n]|[-\N{EN DASH}\N{EM DASH}](?=\s)")
"""A clause mark: a comma, semicolon, colon, bracket, "!", a line end, or a hyphen or dash
followed by a space. A sure sentence end bounds a clause too; an unsure period is none."""
_TOKEN: Final = re.compile(rf"[\w{_APOSTROPHES}]+")
_NEGATED_LEAD: Final = re.compile(
    rf"(?:\bno\s+longer|([\w{_APOSTROPHES}]+))(?:\s+{_LINK})*\s+\Z", re.IGNORECASE
)
_NONE_OPENS: Final = re.compile(
    rf"\s*(?:no|nothing|none|neither)\b(?:\s+[\w{_APOSTROPHES}]+){{0,2}}?(?:\s+{_LINK})*\s*\Z",
    re.IGNORECASE,
)
_SUPERSEDED_LEAD: Final = re.compile(
    rf"\b(?:was|were|originally|previous(?:ly)|had\s+been|used\s+to)(?:\s+{_LINK})*\s+\Z",
    re.IGNORECASE,
)
_REMINDER: Final = re.compile(
    rf"\s*(?:don[{_APOSTROPHES}]t\s+forget|do\s+not\s+forget|remember|reminder)"
    r"(?:\s+that)?\b(?:\s*:)?",
    re.IGNORECASE,
)
_MODAL: Final = re.compile(
    r"\b(?:can|could|must|shall|should|would"
    r"|(?:ought|supposed|expected|going|have|has|had|need|needs)\s+to)\b",
    re.IGNORECASE,
)
"""A modal or semi-modal verb other than "will"; "may" and "might" are hedge words."""
_SUBJECT_PRONOUNS: Final = frozenset({"i", "we", "you", "he", "she", "they"})
_DETERMINERS: Final = frozenset({"the", "a", "an", "your", "our", "this", "that", "these", "those"})
_SENTENCE_ADVERB: Final = re.compile(
    rf"\b([A-Za-z]+ly)\s+(?:{'|'.join(sorted(_DETERMINERS))})\b", re.IGNORECASE
)
"""A word ending in "ly" followed by a determiner ("Hopefully the essay")."""
_INSTRUCTION_VERBS: Final = frozenset({"multiply", "apply", "supply", "reply", "comply"})
"""Verbs ending in "ly" that open a direct instruction ("Multiply the fractions"): as a lead's
first word, none of them is a sentence adverb."""
_CLOCK: Final = r"(?:at|by)\s+(?:\d{1,2}(?::\d{2})?\s*(?:a\.m\.|p\.m\.|am|pm)?|noon|midnight)(?!\w)"
_LEADING_CLOCK: Final = re.compile(_CLOCK, re.IGNORECASE)
_AFTER_DATE: Final = re.compile(
    rf"(?:\s*,\s*|\s+)\d+\s+(?:{'|'.join(_COUNTING_NOUNS)})\b|\s+{_CLOCK}", re.IGNORECASE
)
"""A count (a number and a counting noun) after a space, a comma or a line end, or a clock time
after a space, right after a deadline's date."""
_CONTROL: Final = re.compile(r",\s*not\s+", re.IGNORECASE)
_CONTROL_MORE: Final = re.compile(r"\s*,\s*(?:or|and)\s+|\s+(?:or|and)\s+|\s*,\s*", re.IGNORECASE)
_SENTENCE_END: Final = re.compile(r"[.!]*[\"')\]]*\s*")
_LY: Final = re.compile(rf"\b([A-Za-z]+ly)(?:\s+{_LINK})*\s+\Z", re.IGNORECASE)
_SHORT_WEEKDAYS: Final = {
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_JOINER: Final = re.compile(
    r"\s*,\s*(?:or|and)\s+|\s*,\s*|\s+(?:or|and|&|to|through|thru)\s+"
    r"|\s*[/&\-\N{EN DASH}\N{EM DASH}]\s*",
    re.IGNORECASE,
)
_FRAGMENT: Final = re.compile(
    rf"(?P<weekday>{'|'.join([*_WEEKDAYS, *sorted(_SHORT_WEEKDAYS, key=len, reverse=True)])})\b\.?"
    rf"|(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?![/:])(?!\s+(?:{'|'.join(_COUNTING_NOUNS)})\b)",
    re.IGNORECASE,
)
_PRONOUNS: Final = frozenset({"it", "this", "that", "they"})
_CONTRACTED: Final = re.compile(rf"[{_APOSTROPHES}](?:s|re)\Z", re.IGNORECASE)
"""The "'s" or "'re" after a pronoun, counted as the pronoun and a linking word."""
_PRONOUN_CUE: Final = re.compile(
    rf"\b(?:it|this|that|they|these|those)(?:[{_APOSTROPHES}](?:s|re))?"
    rf"(?:(?:\s+{_LINK})*\s+now)?\s+\Z",
    re.IGNORECASE,
)
"""A pronoun (it, this, that, they, these or those, with "'s" or "'re" or not) right before a
cue, or right before linking words and "now" right before it, whatever words come before."""
_NOW_CUE: Final = re.compile(rf"\bnow(?:\s+{_LINK})*\s+\Z", re.IGNORECASE)
""""now" right before a cue, or right before linking words right before it."""
_FILLER: Final = frozenset(
    {"the", "a", "an", "this", "that", "these", "those", "your", "our", "now"} | _LINK_WORDS
)
"""Determiners, linking words and "now": no word of a lead that names an assignment."""
_REMARK_WORDS: Final = frozenset({"note", "update", "correction", "reminder", "sorry"})
"""Words that make a lead-in a remark about the task before it and no assignment's name."""


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
class Milestone:
    """A date one of a task's several deadline statements could mean, with that statement's
    label, the words its clause names before its cue without joining words, punctuation or a
    leading clock time: "Draft" and "final copy" in "Draft due Wednesday and final copy due
    Friday". A statement that could mean several dates gives one milestone per date, each with
    the same words, in date order."""

    words: str
    day: date


@dataclass(frozen=True)
class NoteTask:
    """One task read from the notes of a paste.

    ``name`` is a candidate title and ``words`` the task's source words with
    its supporting text, as written, paragraphs joined by a blank line.
    ``first_day`` is the first school day it is listed under and ``seen_on``
    every such day, ascending. ``deadline`` is set only when the task is one
    complete, affirmative deadline sentence meaning one date (the module's
    rules); ``deadline_words`` is the task's one statement neither negated nor
    superseded, as written, where it has exactly one. For a task with no
    deadline, ``choices`` holds the dates its statements neither negated nor
    superseded could mean, with their fragments, and ``named`` every date its
    other date expressions could mean outside every statement (in a task holding
    no statement, only the dates one expression settles); both are offered
    unselected, each date once. For a task with a deadline, ``choices`` is empty
    and ``named`` holds the dates its words name outside every statement, each
    naming one date, less its control clause's dates. ``listed_after`` holds the
    school days the task is listed under after its deadline. ``line`` is the
    line of the note it was first listed under. ``milestones`` holds, for a task
    with several statements neither negated nor superseded whose labels are all
    clean, each date one of them could mean with its label; a milestone's date
    is never the deadline, a choice or a named date. A task that says no
    homework is assigned keeps no milestone.
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
    milestones: tuple[Milestone, ...] = ()


@dataclass(frozen=True)
class _Statement:
    """A deadline statement in a piece of text, with every date it could mean.

    No date means "due" alone. ``negated`` is the cue-only negation the
    no-homework rule reads; ``governed`` says the statement is negated or
    superseded under the module's rules, or, where its sentence says no
    homework before its cue, carries ``negated``. ``notice`` says that sentence
    says no homework before the cue. ``fragments`` holds the dates its fragments
    add. ``lead`` is the words its deadline test reads before the cue,
    ``complete`` whether the sentence is the complete affirmative statement the
    deadline needs, and ``control`` its control clause's dates. ``label`` is its
    milestone label and ``title`` the title it gives as a task's first statement
    when it is negated or superseded, or empty.
    """

    start: int
    end: int
    words: str
    dates: tuple[date, ...]
    negated: bool = False
    governed: bool = False
    notice: bool = False
    fragments: tuple[date, ...] = ()
    lead: str = ""
    complete: bool = False
    control: tuple[date, ...] = ()
    label: str = ""
    title: str = ""


@dataclass(frozen=True)
class _Unit:
    """A paragraph, or one sentence of a paragraph that was split, as read.

    ``statements`` holds the statements neither negated nor superseded and
    ``every`` all of them. ``named`` holds the dates named outside every
    statement where each names one date, and ``mentioned`` every date any
    expression outside every statement could mean. ``sentences`` counts its
    sentences by sure ends. ``joined`` says it joined the task before it with a
    statement of its own. ``heading`` is the title its lead-in gives where that
    lead-in names an assignment apart from the task before it, or empty.
    """

    paragraph: int
    start: int
    end: int
    text: str
    statements: tuple[_Statement, ...]
    named: tuple[date, ...]
    unsure: bool = False
    every: tuple[_Statement, ...] = ()
    mentioned: tuple[date, ...] = ()
    sentences: int = 1
    joined: bool = False
    heading: str = ""

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
    milestones: tuple[Milestone, ...] = ()


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


def _weekday_dates(number: int, day: date, *, following: bool = False) -> tuple[date, ...]:
    """Every date a weekday could mean under the school day: the next one, both the school day
    and a week later when it is the school day's weekday, and two after "next"."""
    ahead = (number - day.weekday()) % 7
    if following:
        later = day + timedelta(days=ahead or 7)
        return (later, later + _WEEK)
    if ahead == 0:
        return (day, day + _WEEK)
    return (day + timedelta(days=ahead),)


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
    could = _weekday_dates(number, day, following=bool(weekday["next"]))
    beside = _BESIDE.match(text, weekday.end())
    explicit = _explicit_at(text, beside.end() if beside else weekday.end(), day)
    if explicit is None:
        return weekday.end(), could
    end, exact = explicit
    if exact.weekday() == number:
        return end, (exact,)
    return end, tuple(sorted({exact, *could}))


def _or_after(
    text: str, expression: tuple[int, tuple[date, ...]], day: date
) -> tuple[int, tuple[date, ...]]:
    """A date expression with the ones listed after it, each after a comma, "or" or ", or",
    up to the last one joined by "or" or ", or": where that last one ends and every date any
    of them could mean, in order, each once. Expressions after the last "or", or listed with
    no "or", are not joined, and neither is one after "and". With none joined, the
    expression."""
    end, dates = expression
    joined: set[date] = set(dates)
    listed: set[date] = set()
    at = end
    while True:
        separator = _OR.match(text, at)
        if separator is not None:
            after = _date_at(text, separator.end(), day)
            if after is not None:
                at = end = after[0]
                joined |= listed | set(after[1])
                listed = set()
                continue
        separator = _LISTED.match(text, at)
        after = _date_at(text, separator.end(), day) if separator else None
        if after is None:
            break
        at = after[0]
        listed |= set(after[1])
    return end, tuple(sorted(joined))


def _statements(text: str, day: date) -> tuple[_Statement, ...]:
    """Every deadline statement in the text, in order.

    "due" or "by" followed by a date expression, "due on" or "due by" followed
    by one, or "due" where the end of the text, a line end or punctuation comes
    next. Date expressions listed after the first and joined by a final "or"
    make one statement with the dates of all of them. "by" with no date expression after it is
    not one, and neither is "due" before any other word. A cue negated right
    before it makes a negated statement.
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
                end, dates = _or_after(text, expression, day)
                break
        else:
            if not is_due or _NOTHING_AFTER.match(text, cue.end()) is None:
                continue
        negated = _NEGATED.search(text, 0, cue.start()) is not None
        found.append(_Statement(cue.start(), end, text[cue.start() : end], dates, negated))
        covered = end
    return tuple(found)


def _named(
    text: str, statements: Sequence[_Statement], day: date, *, every_date: bool = False
) -> tuple[date, ...]:
    """The dates the text names outside its deadline statements, where each names one date,
    or with ``every_date``, every date each expression there could mean."""
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
        if len(could) == 1 or every_date:
            dates.extend(could)
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


def _sure_spans(text: str, statements: Sequence[_Statement]) -> list[tuple[int, int]]:
    """Where each sentence of a piece starts and ends, split at sure sentence ends only."""
    spans: list[tuple[int, int]] = []
    start = 0
    for end in _END.finditer(text):
        if any(each.start < end.start() and end.end() <= each.end for each in statements):
            continue
        if _unsure(text, start, end):
            continue
        spans.append((start, end.start("gap")))
        start = end.end()
    spans.append((start, len(text)))
    return spans


def _negation(word: str, after: str = "") -> bool:
    """Whether a word is a negation word; "no" before a period or a digit is not ("No. 3")."""
    low = word.lower().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    if low == "no" and re.match(r"\s*(?:\.|\d)", after):
        return False
    return low in _NEGATION_WORDS or low.endswith("n't")


def _holds_negation(text: str) -> bool:
    """Whether the words hold a negation word or "no" then "longer"."""
    if re.search(r"\bno\s+longer\b", text, re.IGNORECASE):
        return True
    return any(_negation(found[0], text[found.end() :]) for found in _TOKEN.finditer(text))


def _sentence_adverb(words: str) -> bool:
    """Whether a lead holds a sentence adverb: a word ending in "ly" followed by a determiner,
    wherever it stands, other than an instruction verb (multiply, apply, supply, reply or
    comply) that is the lead's first word."""
    first = _TOKEN.search(words)
    opening = -1 if first is None else first.start()
    return any(
        found.start() != opening or found[1].lower() not in _INSTRUCTION_VERBS
        for found in _SENTENCE_ADVERB.finditer(words)
    )


def _unsettled(words: str) -> bool:
    """Whether a lead holds more than an assignment's words and plain linking words: a modal
    or semi-modal verb other than "will", a subject pronoun (I, we, you, he, she, they)
    followed by a word that is not a linking word, or a sentence adverb (a word ending in
    "ly" followed by a determiner, other than an instruction verb that is the lead's first
    word)."""
    if _MODAL.search(words) or _sentence_adverb(words):
        return True
    tokens = [token.lower() for token in _TOKEN.findall(words)]
    return any(
        token in _SUBJECT_PRONOUNS and after not in _LINK_WORDS for token, after in pairwise(tokens)
    )


def _clause_start(text: str, statement: _Statement, previous: int, start: int) -> int:
    """Where a statement's clause starts: after the sentence's start, the statement before it
    and the last clause mark before its cue, whichever is latest."""
    marks = [mark.end() for mark in _MARK.finditer(text, start, statement.start)]
    return max([start, previous, *marks])


def _governed(text: str, statement: _Statement, clause: int, end: int) -> bool:
    """Whether a statement is negated or superseded, its sentence ending at ``end``."""
    lead = text[clause : statement.start]
    negated = _NEGATED_LEAD.search(lead)
    if negated is not None and (negated[1] is None or _negation(negated[1])):
        return True
    if _NONE_OPENS.match(lead) or _SUPERSEDED_LEAD.search(lead):
        return True
    mark = _MARK.search(text, statement.end, end)
    return _CHANGE.search(text, statement.end, mark.start() if mark else end) is not None


def _lead_in_end(text: str, start: int, cue: int) -> int:
    """Where a sentence's lead-in ends: after its reminder opening; with none, the sentence's
    start, so any other opening clause stays in the lead with its clause mark."""
    reminder = _REMINDER.match(text, start, cue)
    return start if reminder is None else reminder.end()


def _lead_start(text: str, statement: _Statement, every: Sequence[_Statement], start: int) -> int:
    """Where the lead a deadline test reads starts: after the last negated or superseded
    statement before it in its sentence and the one clause mark right after that statement;
    with none, after the sentence's lead-in."""
    governed = [
        each
        for each in every
        if each.governed and start <= each.start and each.end <= statement.start
    ]
    if not governed:
        return _lead_in_end(text, start, statement.start)
    at = governed[-1].end
    mark = _MARK.search(text, at, statement.start)
    if mark is not None and not text[at : mark.start()].strip(" \t"):
        at = mark.end()
    return at


def _complete(
    text: str, statement: _Statement, lead: int, end: int, day: date
) -> tuple[bool, tuple[date, ...]]:
    """Whether a statement's sentence, ending at ``end``, is the complete affirmative statement a
    deadline needs, with its control clause's dates."""
    words = text[lead : statement.start]
    marks = list(_MARK.finditer(text, lead, statement.start))
    if marks and (len(marks) > 1 or text[marks[-1].end() : statement.start].strip()):
        return False, ()
    if _holds_negation(words) or any(
        pattern.search(words) for pattern in (_SUPERSESSION, _HEDGE, _CONDITIONAL, _CHANGE_VERB)
    ):
        return False, ()
    ly, first = _LY.search(words), _TOKEN.search(words)
    if ly is not None and first is not None and ly.start(1) != first.start():
        return False, ()
    if _unsettled(words):
        return False, ()
    sentence = text[:end]
    at = statement.end
    after = _AFTER_DATE.match(sentence, at)
    if after is not None:
        at = after.end()
    control: list[date] = []
    found = _CONTROL.match(sentence, at)
    expression = _date_at(sentence, found.end(), day) if found else None
    while expression is not None:
        at = expression[0]
        control.extend(expression[1])
        more = _CONTROL_MORE.match(sentence, at)
        expression = _date_at(sentence, more.end(), day) if more else None
    if _SENTENCE_END.fullmatch(sentence, at) is None:
        return False, ()
    return True, tuple(control)


def _fragments(text: str, statement: _Statement, day: date) -> tuple[date, ...]:
    """The dates the fragments joined after a statement's date add, in order."""
    found: list[date] = []
    last = max(statement.dates) if statement.dates else None
    at = statement.end
    while last is not None:
        joiner = _JOINER.match(text, at)
        fragment = _FRAGMENT.match(text, joiner.end()) if joiner else None
        if fragment is None:
            break
        at = fragment.end()
        could: tuple[date, ...] = ()
        if fragment["weekday"]:
            name = fragment["weekday"].lower()
            number = _WEEKDAYS.index(name) if name in _WEEKDAYS else _SHORT_WEEKDAYS[name]
            could = _weekday_dates(number, day)
        else:
            try:
                could = (date(last.year, last.month, int(fragment["day"])),)
            except ValueError:
                could = ()
        found.extend(could)
        last = max(could) if could else last
    return tuple(found)


def _label(text: str, clause: int, statement: _Statement) -> str:
    """A statement's milestone label: its clause's words before its cue, without joining
    words, a leading clock time or a trailing "is" or "are"."""
    words = _past_joining(text[clause : statement.start])
    clock = _LEADING_CLOCK.match(words)
    if clock is not None:
        words = _past_joining(words[clock.end() :])
    return _name_before(words)


def _past_joining(words: str) -> str:
    """The words after the punctuation, spaces and joining words that open them."""
    opening = _JOINING.match(words)
    return words[opening.end() if opening else 0 :]


def _clean(label: str) -> bool:
    """Whether a milestone label is clean: one to six words, none "due", "by" or "or", and no
    negation, supersession or hedge word."""
    words = {word.lower() for word in _TOKEN.findall(label)}
    count = len(_TOKEN.findall(label))
    return (
        1 <= count <= 6
        and not words & {"due", "by", "or"}
        and not _holds_negation(label)
        and _SUPERSESSION.search(label) is None
        and _HEDGE.search(label) is None
    )


def _title(lead: str) -> str:
    """The title a negated or superseded first statement gives: its clause's words before its
    cue less trailing linking, negation and supersession words, when one to eight words remain
    and none is a negation word; otherwise empty."""
    words = _single(lead).rstrip(_TRAILING).split()
    while words:
        low = words[-1].lower()
        pair = (words[-2].lower(), low) if len(words) > 1 else None
        if pair in {("no", "longer"), ("used", "to")}:
            del words[-2:]
            continue
        if not (low in _LINK_WORDS | _SUPERSESSION_WORDS | {"had"} or _negation(low)):
            break
        words.pop()
    if not 1 <= len(words) <= 8 or any(_negation(word) for word in words):
        return ""
    return _cut(" ".join(words))


def _unit(paragraph: int, text: str, start: int, end: int, day: date) -> _Unit:
    """One piece of a paragraph with its deadline statements, each marked, and the dates it
    names. A negated or superseded statement is none of its statements, and no date in it is
    named."""
    piece = text[start:end]
    found = _statements(piece, day)
    spans = _sure_spans(piece, found)
    marked: list[_Statement] = []
    previous = 0
    for statement in found:
        first, last = next((a, b) for a, b in spans if a <= statement.start <= b)
        clause = _clause_start(piece, statement, previous, first)
        notice = _NO_HOMEWORK.search(piece, first, statement.start) is not None
        governed = statement.negated if notice else _governed(piece, statement, clause, last)
        label = _label(piece, clause, statement)
        title = _title(piece[clause : statement.start]) if governed else ""
        marked.append(
            replace(statement, governed=governed, notice=notice, label=label, title=title)
        )
        previous = statement.end
    every: list[_Statement] = []
    for statement in marked:
        if not statement.governed:
            first, last = next((a, b) for a, b in spans if a <= statement.start <= b)
            lead = _lead_start(piece, statement, marked, first)
            complete, control = _complete(piece, statement, lead, last, day)
            statement = replace(
                statement,
                fragments=_fragments(piece[:last], statement, day),
                lead=piece[lead : statement.start],
                complete=complete,
                control=control,
            )
        every.append(statement)
    return _Unit(
        paragraph,
        start,
        end,
        piece,
        tuple(statement for statement in every if not statement.governed),
        _named(piece, found, day),
        every=tuple(every),
        mentioned=_named(piece, found, day, every_date=True),
        sentences=len(spans),
    )


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


def _as_noted(unit: _Unit) -> _Unit:
    """The piece with the statements the cue-only negation leaves, as the no-homework rule,
    the standing and the plain title read them."""
    return replace(unit, statements=tuple(each for each in unit.every if not each.negated))


def _plain_name(unit: _Unit) -> str:
    """A name from the words before the piece's first statement the cue-only negation leaves,
    or from its headline."""
    noted = _as_noted(unit)
    before = _name_before(noted.before) if noted.statements else ""
    return before or _headline(unit.text)


def _holding(units: Sequence[_Unit]) -> int:
    """The place of a task's first piece holding a statement neither negated nor superseded
    that did not join the task before it, or 0 where none does."""
    return next(
        (place for place, unit in enumerate(units) if unit.statements and not unit.joined), 0
    )


def _name(unit: _Unit) -> str:
    """A task's title from its first piece holding a statement: the title its lead-in gives
    where it names an assignment apart from the task before it, the title a negated or
    superseded first statement gives, or the plain name."""
    if unit.heading:
        return unit.heading
    first = unit.every[0] if unit.every else None
    if first is not None and first.governed and first.title:
        return first.title
    return _plain_name(unit)


def _says_none(units: Sequence[_Unit]) -> bool:
    """Whether a task's first piece says no homework is assigned, as the no-homework rule reads
    it."""
    first = _as_noted(units[0])
    return bool(_NO_HOMEWORK.search(_plain_name(first) if first.statements else first.text))


def _reading(units: Sequence[_Unit], paragraphs: Sequence[str]) -> _Reading:
    """One task from the pieces that make it up.

    A task that says no homework is read by the no-homework rule alone. Any other task sets
    a deadline only when it is one complete affirmative sentence meaning one date; otherwise
    it offers its dates unselected, as milestones where every label is clean. Its standing is
    read from its first piece holding a statement by the no-homework rule's reading.
    """
    holding = _holding(units)
    noted = _noted_reading([_as_noted(unit) for unit in units[holding:]], paragraphs)
    words = _words(units, paragraphs)
    if _says_none(units[holding:]):
        return replace(noted, words=words)
    statements = [statement for unit in units for statement in unit.statements]
    only = statements[0] if len(statements) == 1 else None
    name = _name(units[holding])
    sentences = sum(unit.sentences for unit in units)
    if only is not None and sentences == 1 and len(only.dates) == 1 and only.complete:
        named = {day for unit in units for day in unit.named} - set(only.control)
        return _Reading(
            name, words, noted.standing, only.dates[0], only.words, (), tuple(sorted(named))
        )
    labeled = (
        len(statements) > 1
        and not any(unit.joined for unit in units)
        and all(_clean(statement.label) for statement in statements)
    )
    every = [
        (statement, sorted({*statement.dates, *statement.fragments})) for statement in statements
    ]
    milestones = tuple(Milestone(statement.label, day) for statement, days in every for day in days)
    choices = () if labeled else tuple(sorted({day for _, days in every for day in days}))
    held = {*choices, *(milestone.day for milestone in milestones if labeled)}
    return _Reading(
        name=name,
        words=words,
        standing=noted.standing,
        deadline=None,
        deadline_words=only.words if only is not None else None,
        choices=choices,
        named=tuple(sorted(_named_in(units) - held)),
        milestones=milestones if labeled else (),
    )


def _named_in(units: Sequence[_Unit]) -> set[date]:
    """The dates a task with no deadline names: every date each expression outside every
    statement could mean where the task holds a statement, otherwise only the dates that one
    expression settles."""
    if any(unit.every for unit in units):
        return {day for unit in units for day in unit.mentioned}
    return {day for unit in units for day in unit.named}


def _noted_reading(units: Sequence[_Unit], paragraphs: Sequence[str]) -> _Reading:
    """One task as the no-homework rule reads it, from pieces holding the statements the
    cue-only negation leaves.

    Words that say no homework is assigned are not an assignment unless they also set work:
    an opening word that asks for a task anywhere in them, a deadline statement after more
    words than the ones that say no homework, or a later deadline statement after the one
    that says no homework. Words that set work as well are unclear, and the date each later
    deadline statement settles is named for review. The standing of any other task is read
    the same way: unclear with several statements, a split kept whole as unsure or a first
    statement with no words before it; homework otherwise, or, with no statement, when its
    words open with an opening word.
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


def _correction_word(word: str) -> bool:
    """Whether a word is "now", a linking word, or one of the pronouns it, this, that and they,
    with "'s" or "'re" after it or not."""
    low = word.lower()
    return low == "now" or low in _LINK_WORDS or _CONTRACTED.sub("", low) in _PRONOUNS


def _title_words(before: Sequence[_Unit]) -> set[str]:
    """The words of a task's title, in small letters."""
    return {word.lower() for word in _TOKEN.findall(_name(before[_holding(before)]))}


def _remark(words: Sequence[str]) -> bool:
    """Whether a lead-in's words, in small letters, are a remark and no assignment's name:
    they open with "please", or hold note, update, correction, reminder or sorry."""
    return words[:1] == ["please"] or not _REMARK_WORDS.isdisjoint(words)


def _apart(unit: _Unit, before: Sequence[_Unit]) -> str:
    """The title a piece's lead-in gives where it names an assignment apart from the task
    before it: the lead's words before its last clause mark ahead of a pronoun or "now" that
    stands before the cue, when they are no remark ("Please note:", "Quick update:"), are two
    or more words and share none with that task's title; otherwise empty."""
    lead = unit.statements[0].lead
    cue = _PRONOUN_CUE.search(lead) or _NOW_CUE.search(lead)
    marks = list(_MARK.finditer(lead, 0, cue.start())) if cue else []
    if not marks:
        return ""
    words = [word.lower() for word in _TOKEN.findall(lead[: marks[-1].start()])]
    if _remark(words) or len(words) < 2 or _title_words(before) & set(words):
        return ""
    return _cut(_single(lead[: marks[-1].start()]).strip(_TRAILING))


def _joins(unit: _Unit, before: Sequence[_Unit]) -> bool:
    """Whether a piece with a statement of its own joins the task before it as a correction:
    its first statement's sentence says no homework nowhere before its cue, and its lead,
    after any lead-in, holds only "now", the pronouns and linking words; or its lead-in names
    no assignment apart from that task, and a pronoun stands right before its cue, or right
    before linking words and "now" right before it, whatever words come before the pronoun;
    or "now" stands right before its cue or before linking words right before it, and nothing
    is left once the words up to the lead's last clause mark, determiners, linking words,
    "now", words ending in "ly" and the words of the task's title are removed."""
    first = unit.statements[0]
    if first.notice:
        return False
    if unit.only_statement or all(_correction_word(each) for each in _TOKEN.findall(first.lead)):
        return True
    if _apart(unit, before):
        return False
    if _PRONOUN_CUE.search(first.lead) is not None:
        return True
    if _NOW_CUE.search(first.lead) is None:
        return False
    marks = list(_MARK.finditer(first.lead))
    rest = first.lead[marks[-1].end() :] if marks else first.lead
    title = _title_words(before)
    return all(
        low in _FILLER or low.endswith("ly") or low in title
        for low in (word.lower() for word in _TOKEN.findall(rest))
    )


def _leads_in(head: Sequence[_Unit], paragraphs: Sequence[str]) -> bool:
    """Whether the pieces at the head of a note join the task after them: they hold no deadline
    statement and no date expression, and read on their own they are unclear, neither homework
    nor a note that says no homework is assigned."""
    if any(unit.every or unit.mentioned for unit in head):
        return False
    noted = _noted_reading([_as_noted(unit) for unit in head], paragraphs)
    return noted.standing is Standing.UNCLEAR


def _read_block(block: NoteBlock) -> list[_Reading]:
    """The tasks of one note, in the order it writes them.

    A piece with a deadline statement of its own starts a task, unless it is only a deadline
    statement or joins the task before it as a correction. A piece with none joins the task
    before it. Pieces with none at the head of a note join the task of its first piece
    holding a statement when they name no date, read on their own as unclear, and that piece
    does not say no homework; otherwise they are the first task. A first such piece that is
    only a deadline statement joins them, and the title is read from them. A suspect note is
    one unclear task holding all of it, with no date read from it.
    """
    paragraphs = [paragraph.strip() for paragraph in block.paragraphs if paragraph.strip()]
    if not paragraphs:
        return []
    if block.suspect:
        words = "\n\n".join(paragraphs)
        return [_Reading(_headline(paragraphs[0]), words, Standing.UNCLEAR, None, None, (), ())]
    tasks: list[list[_Unit]] = []
    head: list[_Unit] = []
    for paragraph, text in enumerate(paragraphs):
        for unit in _units(paragraph, text, block.day):
            if not tasks and not unit.statements:
                head.append(unit)
            elif not tasks:
                if head and unit.only_statement:
                    tasks.append([*head, replace(unit, joined=True)])
                elif head and _leads_in(head, paragraphs) and not _says_none([unit]):
                    tasks.append([*head, unit])
                elif head:
                    tasks.append(head)
                    tasks.append([unit])
                else:
                    tasks.append([unit])
                head = []
            elif not unit.statements or _joins(unit, tasks[-1]):
                tasks[-1].append(replace(unit, joined=bool(unit.statements)))
            else:
                tasks.append([replace(unit, heading=_apart(unit, tasks[-1]))])
    if head:
        tasks.append(head)
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
        milestones=reading.milestones,
    )
