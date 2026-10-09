# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Current values of a class and term, read from its stored reports by one pure function, and the
preview of "Use saved values from this report as current".

The projection takes the reports (each with its acceptance order, use, number of result rows and
completeness), their observations and their row records, and gives each target's current value,
a result's last showing, its due date and the newest report that supports its absence. The
preview runs the same function twice: over the stored reports, and over them with a copy of the
source added as the next current report, named ``"new"``. Every difference between the two is
an effect, from what to what, unless reading ``"new"`` as the source makes it none. The canonical
form of the preview is JSON with sorted keys and fixed separators, and its SHA-256 digest is what
a confirmation carries.

A parent's assertion belongs to the original reading: a copy the class-details action made finds
its original through the action's copy list, so the assertion shows beside the original and every
copy, and never beside an independent reading. It is a mark beside the cells, never in them.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Final

from blossom.grades.draft import Presence
from blossom.grades.review import (
    TERM_KEY,
    Cell,
    ClassRecord,
    Compared,
    CurrentValue,
    CurrentValues,
    DueFrom,
    ReportAt,
    compared,
)

NEW: Final = "new"
"""The report the class-details action would make, as the projection and the preview name it."""
EXPLICIT: Final = frozenset({"answer", "chosen"})
"""How a row record counts as the parent's explicit answer, whether its row was shown or
accepted."""

Observed = tuple[str, str, str, Mapping[str, Cell]]
"""An observation: its kind (``term``, ``category`` or ``result``), its target, its report and
its cells."""
Decided = tuple[str, str, str | None, str, str | None]
"""A row record: its report, its row evidence, the result it names (None for "different"), how,
and the candidates a "different" one turned down."""
Copies = Mapping[str, tuple[str, frozenset[tuple[str, str]]]]
"""By each report the class-details action made, its source report and the kind and target of
each observation it copied."""
Recorded = tuple[str, str, str, str, str, str, str]
"""A correction record: its original's report, kind and target, the field, the text recorded,
``reported`` or ``withdrawn``, and its kind."""
Marks = Mapping[tuple[str, str, str], Mapping[str, Cell]]
"""By observation (kind, target, report), each field's standing parent assertion."""


def original_of(kind: str, target: str, report: str, copies: Copies) -> str:
    """The report of the reading ``report`` holds for ``kind`` and ``target``: while an action
    made the report and copied that observation, its source; the first report that didn't."""
    while report in copies and (kind, target) in copies[report][1]:
        report = copies[report][0]
    return report


def apply_corrections(
    observed: tuple[Observed, ...], corrections: tuple[Recorded, ...], copies: Copies
) -> Marks:
    """Each observation's standing parent assertions, from its original's records in sequence
    order: the latest record of each field decides, and a withdrawal leaves none."""
    standing: dict[tuple[str, str, str], dict[str, Cell]] = {}
    for report_id, kind, target, name, text, presence, how in corrections:
        if how != "parent_assertion":
            continue
        fields = standing.setdefault((kind, target, report_id), {})
        if presence == "withdrawn":
            fields.pop(name, None)
        else:
            fields[name] = (Presence.REPORTED, text)
    marks: dict[tuple[str, str, str], Mapping[str, Cell]] = {}
    for kind, target, report_id, _ in observed:
        found = standing.get((kind, target, original_of(kind, target, report_id, copies)))
        if found:
            marks[kind, target, report_id] = dict(found)
    return marks


@dataclass(frozen=True)
class ScopeHeld:
    """What a class and term store: each report's acceptance order, use and number of result
    rows; whether each report is complete; every observation; every row record; and the
    parent's assertions on each observation."""

    reports: Mapping[str, tuple[int, str, int]]
    complete: Mapping[str, bool]
    """A report's completeness by the one function: at least one acceptance or a making action,
    and each of them complete. A report missing here is incomplete."""
    observed: tuple[Observed, ...]
    decided: tuple[Decided, ...]
    marks: Marks = field(default_factory=dict)
    """Each observation's standing parent assertions, kept apart from its cells."""


def project(held: ScopeHeld) -> ClassRecord:
    """Each target's current value from the current report with the highest acceptance order
    that supplied it, a result's last showing, absence and due date, and what matching reads."""
    reports = held.reports
    observed = sorted(held.observed, key=lambda one: reports[one[2]][0])
    current: dict[str, dict[str, CurrentValue]] = {"term": {}, "category": {}, "result": {}}
    once_current: dict[str, set[Compared]] = {}
    latest: dict[str, CurrentValue] = {}
    newest: dict[str, ReportAt] = {}
    # A result's due date comes from the newest observation that captured its Due cell, blank or
    # unreadable included: a current one for the date shown, one in any report for matching.
    shown_due: dict[str, DueFrom] = {}
    matched_due: dict[str, DueFrom] = {}

    def showing(target: str, report_id: str) -> None:
        order, use, _ = reports[report_id]
        if use == "current" and order > newest.get(target, ReportAt("", 0)).order:
            newest[target] = ReportAt(report_id, order)

    for kind, target, report_id, cells in observed:
        order, use, _ = reports[report_id]
        value = CurrentValue(
            cells, report_id, order, asserted=held.marks.get((kind, target, report_id), {})
        )
        if kind == "result":
            latest[target] = value
            if cells["due"][0] is not Presence.NOT_CAPTURED:
                matched_due[target] = DueFrom(cells["due"], ReportAt(report_id, order))
                if use == "current":
                    shown_due[target] = matched_due[target]
        if use == "current":
            current[kind][target] = value
            once_current.setdefault(target, set()).add(compared(cells))
        showing(target, report_id)
    decided: dict[str, set[str]] = {}
    explicit: dict[str, set[str]] = {}
    different: dict[str, set[str]] = {}
    resolved: Counter[str] = Counter()
    for report_id, evidence, result, how, rejected in held.decided:
        if result is None:
            different.setdefault(evidence, set()).add(str(rejected))
            continue
        decided.setdefault(evidence, set()).add(result)
        if how in EXPLICIT:
            explicit.setdefault(evidence, set()).add(result)
        resolved[report_id] += 1
        showing(result, report_id)
    # A report supports "Not shown in this report" only when it is current, complete, and each
    # of its result rows has a record naming a result.
    proving = sorted(
        (
            ReportAt(report_id, order)
            for report_id, (order, use, rows) in reports.items()
            if use == "current"
            and held.complete.get(report_id, False)
            and resolved[report_id] == rows
        ),
        key=lambda report: report.order,
    )
    for result, value in current["result"].items():
        shown = newest[result]
        absent = [report for report in proving if report.order > shown.order]
        current["result"][result] = replace(
            value,
            last_shown=shown,
            not_shown=absent[-1] if absent else None,
            due=shown_due.get(result),
        )
    return ClassRecord(
        current=CurrentValues(
            term=current["term"].get(TERM_KEY),
            categories=current["category"],
            results=current["result"],
        ),
        once_current={target: frozenset(values) for target, values in once_current.items()},
        latest={
            result: replace(value, due=matched_due.get(result)) for result, value in latest.items()
        },
        decided={evidence: frozenset(results) for evidence, results in decided.items()},
        newest={target: report.order for target, report in newest.items()},
        explicit={evidence: frozenset(results) for evidence, results in explicit.items()},
        different={evidence: frozenset(texts) for evidence, texts in different.items()},
    )


def with_copy(held: ScopeHeld, source: str) -> ScopeHeld:
    """``held`` with the report the action would make: next in acceptance order, current, the
    source's number of result rows and completeness, a copy of each of its observations with
    their assertions, and a ``same_capture`` copy of each of its row records that names a
    result."""
    order = max(order for order, _, _ in held.reports.values()) + 1
    rows = held.reports[source][2]
    return ScopeHeld(
        reports={**held.reports, NEW: (order, "current", rows)},
        complete={**held.complete, NEW: held.complete.get(source, False)},
        observed=held.observed
        + tuple(
            (kind, target, NEW, cells)
            for kind, target, report_id, cells in held.observed
            if report_id == source
        ),
        decided=held.decided
        + tuple(
            (NEW, evidence, result, "same_capture", None)
            for report_id, evidence, result, _, _ in held.decided
            if report_id == source and result is not None
        ),
        marks={
            **held.marks,
            **{
                (kind, target, NEW): fields
                for (kind, target, report_id), fields in held.marks.items()
                if report_id == source
            },
        },
    )


Placed = tuple[str, list[list[str]]] | None
"""Where a value comes from in the canonical form: its report and its cells, or None."""


@dataclass(frozen=True)
class ValueEffect:
    """A value the action would make current: its kind and target, the current value it
    replaces (None for a first one), and the value it would be, from the new report."""

    kind: str
    target: str
    before: CurrentValue | None
    after: CurrentValue


@dataclass(frozen=True)
class ShowingEffect:
    """A result's last showing, or the report that supports its absence, from what to what; a
    report ID, ``"new"``, or None."""

    result: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class DueEffect:
    """A result's shown due date with the report it came from, from what to what; None when no
    current observation captured it."""

    result: str
    before: DueFrom | None
    after: DueFrom | None


@dataclass(frozen=True)
class CurrentPreview:
    """What "Use saved values from this report as current" would do now, under a fresh action
    ID: the source report, its acceptances and making action, the revision, the new report's
    completeness, every effect, and the canonical form with its digest. ``empty`` offers no
    action."""

    action_id: str
    source: str
    acceptances: tuple[str, ...]
    made_by: str | None
    revision: int
    complete: bool
    values: tuple[ValueEffect, ...]
    last_shown: tuple[ShowingEffect, ...]
    not_shown: tuple[ShowingEffect, ...]
    due: tuple[DueEffect, ...]
    canonical: str
    digest: str

    @property
    def empty(self) -> bool:
        """Whether nothing would change once ``"new"`` is read as the source."""
        return not (self.values or self.last_shown or self.not_shown or self.due)


@dataclass(frozen=True)
class SourceOf:
    """The scope and the source of a preview: her student ID, the class, the folded term, the
    revision, the source report, the acceptances whose report it is, and the action that made
    it, if any."""

    student_id: str
    class_id: str
    term: str
    revision: int
    report_id: str
    acceptances: tuple[str, ...]
    made_by: str | None


def _targets(values: CurrentValues) -> dict[tuple[str, str], CurrentValue]:
    targets = {("category", key): value for key, value in values.categories.items()}
    targets.update({("result", key): value for key, value in values.results.items()})
    if values.term is not None:
        targets["term", TERM_KEY] = values.term
    return targets


def _cells(cells: Mapping[str, Cell]) -> list[list[str]]:
    return [[field, presence.value, text] for field, (presence, text) in cells.items()]


def _placed(value: CurrentValue | None) -> Placed:
    return None if value is None else (value.report_id, _cells(value.cells))


def _due_placed(due: DueFrom | None) -> tuple[str, list[str]] | None:
    """A due date in the canonical form: its report and its cell, or None."""
    return None if due is None else (due.report.report_id, ["due", due.cell[0].value, due.cell[1]])


def preview_of(held: ScopeHeld, source: SourceOf, action_id: str) -> CurrentPreview:
    """The effects of copying ``source`` as the next current report, by ``project`` over
    ``held`` with and without the copy. An effect stays only when it differs once ``"new"`` is
    read as the source, so a source change with the same text is one."""
    before = project(held)
    after = project(with_copy(held, source.report_id))

    def as_source(report_id: str | None) -> str | None:
        return source.report_id if report_id == NEW else report_id

    old, new = _targets(before.current), _targets(after.current)
    values = tuple(
        ValueEffect(kind, target, old.get((kind, target)), value)
        for (kind, target), value in sorted(new.items())
        if (as_source(value.report_id), _cells(value.cells)) != _placed(old.get((kind, target)))
    )
    results = sorted(set(before.current.results) | set(after.current.results))

    def moved(fact: str) -> tuple[ShowingEffect, ...]:
        effects = []
        for result in results:
            was, now = before.current.results.get(result), after.current.results.get(result)
            from_report = None if was is None else _report_of(getattr(was, fact))
            to_report = None if now is None else _report_of(getattr(now, fact))
            if as_source(to_report) != from_report:
                effects.append(ShowingEffect(result, from_report, to_report))
        return tuple(effects)

    last_shown, not_shown = moved("last_shown"), moved("not_shown")

    def due_of(record: ClassRecord, result: str) -> DueFrom | None:
        value = record.current.results.get(result)
        return None if value is None else value.due

    due = []
    for result in results:
        was, now = due_of(before, result), due_of(after, result)
        placed = _due_placed(now)
        if (None if placed is None else (as_source(placed[0]), placed[1])) != _due_placed(was):
            due.append(DueEffect(result, was, now))
    complete = held.complete.get(source.report_id, False)
    body = {
        "scope": [source.student_id, source.class_id, source.term],
        "revision": source.revision,
        "source": {
            "report": source.report_id,
            "acceptances": sorted(source.acceptances),
            "action": source.made_by,
        },
        "complete": complete,
        "values": [
            [effect.kind, effect.target, _placed(effect.before), _placed(effect.after)]
            for effect in values
        ],
        "last_shown": [[effect.result, effect.before, effect.after] for effect in last_shown],
        "not_shown": [[effect.result, effect.before, effect.after] for effect in not_shown],
        "due": [
            [effect.result, _due_placed(effect.before), _due_placed(effect.after)] for effect in due
        ],
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return CurrentPreview(
        action_id=action_id,
        source=source.report_id,
        acceptances=tuple(sorted(source.acceptances)),
        made_by=source.made_by,
        revision=source.revision,
        complete=complete,
        values=values,
        last_shown=last_shown,
        not_shown=not_shown,
        due=tuple(due),
        canonical=canonical,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _report_of(report: ReportAt | None) -> str | None:
    return None if report is None else report.report_id


@dataclass(frozen=True)
class CurrentPage:
    """What a confirmation of the preview posts: its action ID, its source report and the
    digest of its canonical form."""

    action_id: str
    source: str
    digest: str


@dataclass(frozen=True)
class MadeCurrent:
    """A confirmed action: its ID, the source report, the report it made, and the digest of the
    preview it confirmed."""

    action_id: str
    source: str
    report_id: str
    digest: str


@dataclass(frozen=True)
class ActionRecorded:
    """The action ID was confirmed before: its recorded outcome, and, when the posted source or
    digest differs from the recorded one, a fresh preview for the posted source. Nothing was
    written."""

    made: MadeCurrent
    fresh: "CurrentPreview | ReportNotSaved | None" = None


@dataclass(frozen=True)
class PreviewRevised:
    """The source's acceptances, the revision or the preview changed: the preview as it reads
    now, under a fresh action ID, to confirm again. Nothing was written."""

    preview: CurrentPreview


@dataclass(frozen=True)
class NothingToChange:
    """The source already supplies the same values and presence: nothing was written, and the
    revision stays."""


@dataclass(frozen=True)
class ReportNotSaved:
    """The report isn't saved in her class and term any more, or never was: nothing was
    written."""


ActionOutcome = MadeCurrent | ActionRecorded | PreviewRevised | NothingToChange | ReportNotSaved
