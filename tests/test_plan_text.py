"""The saved plan taken apart for reading, along the composer's own shapes, losing nothing."""

from datetime import time

from blossom.agent.compose import compose_draft, one_line
from blossom.clock import spoken_time
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.plan_checks import check_plan
from blossom.plan_text import present_plan
from blossom.plans import DailyPlan, Deferral, PlanBlock
from tests.support import ESSAY, PLAN_DATE, PROBLEM_SET, ZONE


def composed() -> str:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first, while the afternoon is quiet",
            )
        ],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
    )
    verdict = CriticVerdict(
        findings=[
            CriterionFinding(
                criterion=Criterion.SUPPORT_RULES,
                critique="no rules were given",
                judgment=Judgment.CANNOT_TELL,
            )
        ]
    )
    return compose_draft(
        draft_id="draft:test",
        plan=plan,
        assignments=[ESSAY, PROBLEM_SET],
        verification=check_plan(plan, due_in_window=[ESSAY, PROBLEM_SET], zone=ZONE),
        verdict=verdict,
        settled=False,
    ).body


def test_a_composed_draft_comes_apart_along_its_shapes() -> None:
    text = present_plan(composed())

    assert text.title == "Plan for Wednesday, August 19, 2026"
    assert text.notes == ["The reviewer did not settle on this plan. Its notes are at the end."]
    assert [(block.span, block.item) for block in text.blocks] == [
        ("4:30 PM to 5:30 PM", "Canal Era comparison essay (World History, due Aug 21)")
    ]
    assert text.blocks[0].rationale == "the essay first, while the afternoon is quiet"
    assert [section.title for section in text.sections] == ["Waiting for another day"]
    assert text.sections[0].items[0].text.startswith("Quadratic modeling problem set")
    assert text.review is not None
    finding = next(item for item in text.review.items if item.label)
    assert finding.label == "support rules (could not assess)"
    assert finding.text == "no rules were given"
    assert text.review.items[0].text.startswith("The reviewer did not consider:")
    assert text.other == []


def test_nothing_in_the_saved_text_is_lost() -> None:
    """Every line of the saved text is on the page in one of the presenter's parts."""
    body = composed()
    text = present_plan(body)
    rendered = [text.title, *text.notes, *text.other]
    for block in text.blocks:
        rendered.extend([f"{block.span}, set aside for {block.item}", block.rationale])
    for section in [*text.sections, *([text.review] if text.review else [])]:
        rendered.append(section.heading)
        rendered.extend(f"{i.label}: {i.text}" if i.label else i.text for i in section.items)

    for line in body.splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        assert stripped.removeprefix("- ") in rendered, stripped


def test_the_two_space_block_shape_and_an_unknown_line_are_kept_whole() -> None:
    two_space = "\n".join(
        [
            "Plan for Wednesday, August 19, 2026",
            "",
            "A line in a shape nobody has seen.",
            "",
            "16:30 to 17:30  Canal Era comparison essay (World History, due Aug 21)",
            "    the essay first",
            "",
            "Due dates worth checking with the school:",
            "- Canal Era comparison essay (World History, due Aug 21)",
        ]
    )
    text = present_plan(two_space)

    assert text.blocks[0].span == "16:30 to 17:30"
    assert text.blocks[0].rationale == "the essay first"
    assert text.other == ["A line in a shape nobody has seen."]
    assert text.sections[0].title == "Due dates worth checking with the school"
    assert text.sections[0].items[0].label is None


def test_a_value_with_a_line_break_inside_stays_with_its_shape() -> None:
    """A rationale, a reason, or a critique written across lines continues in place."""
    saved = "\n".join(
        [
            "Plan for Wednesday, August 19, 2026",
            "",
            "4:30 PM to 5:30 PM, set aside for Canal Era comparison essay "
            "(World History, due Aug 21)",
            "    the essay first,",
            "while the afternoon is quiet",
            "",
            "Waiting for another day:",
            "- Quadratic modeling problem set (Algebra II, due Aug 24): not due",
            "until Monday",
            "",
            "The reviewer's notes:",
            "- sizing (could not assess): an hour may be right",
            "or generous; nothing here says which",
        ]
    )
    text = present_plan(saved)

    assert text.blocks[0].rationale == "the essay first, while the afternoon is quiet"
    assert text.sections[0].items[0].text.endswith("not due until Monday")
    assert text.review is not None
    assert text.review.items[0].text == "an hour may be right or generous; nothing here says which"
    assert text.other == []


def test_the_composer_keeps_each_value_on_one_line() -> None:
    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first,\nwhile the afternoon\n  is quiet",
            )
        ],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due\nuntil Monday")],
    )
    verdict = CriticVerdict(
        findings=[
            CriterionFinding(
                criterion=Criterion.SIZING,
                critique="an hour may be right\nor generous",
                judgment=Judgment.CANNOT_TELL,
            )
        ]
    )
    body = compose_draft(
        draft_id="draft:test",
        plan=plan,
        assignments=[ESSAY, PROBLEM_SET],
        verification=check_plan(plan, due_in_window=[ESSAY, PROBLEM_SET], zone=ZONE),
        verdict=verdict,
        settled=False,
    ).body

    assert "    the essay first, while the afternoon is quiet\n" in body
    assert ": not due until Monday\n" in body
    assert "(could not assess): an hour may be right or generous" in body
    assert present_plan(body).other == []


def test_an_escape_sequence_in_the_text_reads_as_its_character() -> None:
    """A model sometimes writes an em dash as its escape; the page shows the dash, and
    the composer writes the dash into a new draft to begin with."""
    dash = chr(0x2014)
    saved = "\n".join(
        [
            "Plan for Wednesday, August 19, 2026",
            "",
            "The reviewer's notes:",
            "- order (passes): the two errands \\u2014 the signature "
            "and the binder \\u2014 come after",
        ]
    )
    shown = present_plan(saved)
    assert shown.review is not None
    assert (
        shown.review.items[0].text
        == f"the two errands {dash} the signature and the binder {dash} come after"
    )

    plan = DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first \\u2014 while the afternoon is quiet",
            )
        ],
    )
    body = compose_draft(
        draft_id="draft:test",
        plan=plan,
        assignments=[ESSAY],
        verification=check_plan(plan, due_in_window=[ESSAY], zone=ZONE),
        verdict=CriticVerdict(findings=[]),
        settled=True,
    ).body
    assert f"the essay first {dash} while the afternoon is quiet" in body
    assert "\\u2014" not in body


def test_only_printable_characters_are_decoded_and_only_inside_a_part() -> None:
    """A control code or a lone surrogate written as an escape stays as written, so a
    sequence can neither move a line boundary nor break the page's encoding; a pair
    becomes the one character it encodes."""
    saved = "\n".join(
        [
            "Plan for Wednesday, August 19, 2026\\u000ANot a second line",
            "",
            "The reviewer's notes:",
            "- order (passes): one note\\u000Astill one note",
            "- sizing (passes): a smile \\uD83D\\uDE00 and a stray \\uDE00 half",
            "- deferrals (passes): a separator \\u2028 a direction mark \\u202E "
            "a tag \\uDB40\\uDC01",
        ]
    )
    text = present_plan(saved)

    assert text.title == "Plan for Wednesday, August 19, 2026\\u000ANot a second line"
    assert text.review is not None
    assert [item.text for item in text.review.items] == [
        "one note\\u000Astill one note",
        f"a smile {chr(0x1F600)} and a stray \\uDE00 half",
        "a separator \\u2028 a direction mark \\u202E a tag \\uDB40\\uDC01",
    ]
    for item in text.review.items:
        item.text.encode("utf-8")
    assert one_line("a separator \\u2028 stays") == "a separator \\u2028 stays"


def test_the_clock_reads_as_she_does() -> None:
    assert spoken_time(time(0, 5)) == "12:05 AM"
    assert spoken_time(time(9, 0)) == "9:00 AM"
    assert spoken_time(time(12, 0)) == "12:00 PM"
    assert spoken_time(time(17, 40)) == "5:40 PM"
    assert spoken_time(time(23, 59)) == "11:59 PM"
