"""Her homework notes from a fixture set, planted in a blank file the way she and the
family would have made them.

The sample household carries a note that waits with no class yet, and a note
that was added to homework, which the school's portal later spoke of: a due
date read from the portal and the school's instruction, which a parent said
was about the same homework. Each is made through the store's own paths, in the
transaction that seeds a blank file, so the sample holds exactly what those
presses would have left. A file with anything in it is never planted.
"""

from collections.abc import Sequence

from blossom.candidates import candidate_readings, reader
from blossom.captures import CaptureCreated, CapturePromoted, candidate_basis
from blossom.intake import OWN_LINE, PORTAL_CONFIDENCE, identity_basis
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.school_instructions import InstructionSeen, InstructionsSettled
from blossom.sources import SampleNote
from blossom.stores.intake_decisions import DecisionToKeep
from blossom.stores.project_state import ProjectStateStore


def plant_notes(store: ProjectStateStore, notes: Sequence[SampleNote]) -> None:
    """Make each note, add it to homework when the set says so, and put the school's word on
    that homework, in the caller's transaction. Anything refused stops the seeding."""
    for note in notes:
        made = store.create_capture(
            note.capture_id,
            note.text,
            None,
            None,
            authored_by="household",
            channel=SourceChannel.STUDENT_REPORT,
            now=note.written_at,
            today=note.written_on,
        )
        if not isinstance(made, CaptureCreated):
            msg = f"the sample note {note.capture_id} could not be made"
            raise ValueError(msg)
        if note.added is None:
            continue
        added = store.promote_capture(
            note.capture_id,
            note.added,
            expected_revision=1,
            basis=candidate_basis(candidate_readings(store, note.added)),
            choice="new",
            candidates=reader(store),
            authored_by="household",
            channel=SourceChannel.STUDENT_REPORT,
            now=note.written_at,
            today=note.written_on,
        )
        if not isinstance(added, CapturePromoted):
            msg = f"the sample note {note.capture_id} could not be added to homework"
            raise ValueError(msg)
        if note.school_read_at is None or note.school_read_on is None:
            continue
        homework = added.assignment_id
        row = store.one_assignment(homework)
        if row is None:
            msg = f"the sample note's homework {homework} is not on record"
            raise ValueError(msg)
        if note.school_due is not None:
            claim = SourceRecord(
                channel=SourceChannel.LMS,
                asserted_value=note.school_due.isoformat(),
                observed_at=note.school_read_at,
                confidence=PORTAL_CONFIDENCE,
                seen_in=OWN_LINE,
            )
            store.put_on_record([], {homework: [claim]})
        if note.school_instruction is not None:
            seen = InstructionSeen(
                note.school_instruction, SourceChannel.LMS, "assigned", note.school_read_on
            )
            settled = store.settle_school_instructions(
                homework,
                [seen],
                None,
                authored_by="household",
                now=note.school_read_at,
                today=note.school_read_on,
            )
            if not isinstance(settled, InstructionsSettled):
                msg = f"the sample instruction for {homework} could not be kept"
                raise ValueError(msg)
        store.record_intake_decision(
            DecisionToKeep(
                kind="same",
                course=row.course,
                title=row.title,
                due_date=note.school_due,
                lands_on=homework,
                shown=(homework,),
                basis=identity_basis([row], (), {homework}),
            ),
            authored_by="household",
            channel=SourceChannel.PARENT_ENTRY,
            now=note.school_read_at,
            today=note.school_read_on,
        )
