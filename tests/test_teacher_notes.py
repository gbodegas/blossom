# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A teacher's notes read as tasks, one row per expected outcome.

Every note here is made up, in the shapes a school writes them: a subject, the
school day it is listed under, and one or more paragraphs. Each row gives the
notes of one paste and the tasks they must read as; a row names only the fields
it is about, and the task names are always compared in order. The year is 2026,
where November 2nd is a Monday.
"""

from datetime import date

import pytest

from blossom.teacher_notes import (
    NAME_LIMIT,
    Milestone,
    NoteBlock,
    NoteTask,
    Standing,
    read_notes,
)

HOMEWORK, UNCLEAR, NOT_ASSIGNMENT = Standing.HOMEWORK, Standing.UNCLEAR, Standing.NOT_ASSIGNMENT

Want = dict[str, object]


def nov(day: int) -> date:
    """A day of November 2026."""
    return date(2026, 11, day)


def note(subject: str, day: date, *paragraphs: str, suspect: bool = False) -> NoteBlock:
    """One note as the reader hands it over."""
    return NoteBlock(subject=subject, day=day, paragraphs=paragraphs, line=0, suspect=suspect)


def want(name: str, standing: Standing, **fields: object) -> Want:
    """What one task must be: its name, its standing and any other field the row is about."""
    return {"name": name, "standing": standing, **fields}


THREE_IN_ONE = (
    "Study for the vocabulary quiz on Thursday. Map project due on Wednesday. "
    "Timeline poster due Thursday."
)
PRACTICE = "Online practice due Tuesday"
PRACTICE_WEEK = tuple(note("Science", nov(day), PRACTICE) for day in (10, 11, 12, 13))
LONG_WORDS = " ".join(["abcdefghi"] * 30)
PACKET = "Review packet due 11/10- Solutions will be posted Tomorrow, use them wisely."
CLASS_SITE = (
    "Go to the class site- finish notes for Unit 3.1- concentrate on vocabulary and "
    "the example problems you'd like to reference"
)
SLIDES = (
    "Check the class site for the Unit 2 slides to begin preparing for the Unit 2 test "
    "coming up on 11/12."
)
CLASS_TIME = (
    "We will have 10 minutes at the start of class for unfinished problems from the test.",
    "The reassessment is in class.",
    "Notes for the next unit are posted.",
    "Bring nothing extra.",
)
LAST_DAY = (
    "Final day to review for tomorrow's unit assessment. Focus on the study guide. "
    "Mr. Brown is visiting class today."
)

ROWS = [
    pytest.param(
        (note("Humanities", nov(2), THREE_IN_ONE),),
        (
            want(
                "Study for the vocabulary quiz on Thursday",
                HOMEWORK,
                subject="Humanities",
                words="Study for the vocabulary quiz on Thursday.",
                deadline=None,
                deadline_words=None,
                choices=(),
                named=(nov(5),),
                first_day=nov(2),
                seen_on=(nov(2),),
            ),
            want(
                "Map project",
                HOMEWORK,
                words="Map project due on Wednesday.",
                deadline=nov(4),
                deadline_words="due on Wednesday",
                choices=(),
                named=(),
            ),
            want(
                "Timeline poster",
                HOMEWORK,
                words="Timeline poster due Thursday.",
                deadline=nov(5),
                deadline_words="due Thursday",
                listed_after=(),
            ),
        ),
        id="three-sentences-three-tasks",
    ),
    pytest.param(
        (note("Humanities", nov(2), THREE_IN_ONE), note("Humanities", nov(3), THREE_IN_ONE)),
        (
            want(
                "Study for the vocabulary quiz on Thursday",
                HOMEWORK,
                deadline=None,
                named=(nov(5),),
                first_day=nov(2),
                seen_on=(nov(2), nov(3)),
            ),
            want("Map project", HOMEWORK, deadline=nov(4), seen_on=(nov(2), nov(3))),
            want(
                "Timeline poster",
                HOMEWORK,
                deadline=nov(5),
                seen_on=(nov(2), nov(3)),
                listed_after=(),
            ),
        ),
        id="three-tasks-listed-again-the-next-day",
    ),
    pytest.param(
        (note("Science", nov(2), "Online practice Due Tuesday November 3rd"),),
        (
            want(
                "Online practice",
                HOMEWORK,
                deadline=nov(3),
                deadline_words="Due Tuesday November 3rd",
                choices=(),
                named=(),
            ),
        ),
        id="a-weekday-beside-a-month-and-day",
    ),
    pytest.param(
        (note("Art", nov(2), "Please bring a shoebox by next Tuesday, November 3rd to class!"),),
        (
            want(
                "Please bring a shoebox",
                HOMEWORK,
                words="Please bring a shoebox by next Tuesday, November 3rd to class!",
                deadline=None,
                deadline_words="by next Tuesday, November 3rd",
                choices=(nov(3),),
            ),
        ),
        id="by-with-a-date-and-no-due",
    ),
    pytest.param(
        (note("08 Geometry", nov(3), "Quiz on unit two", "Sections 2 and 3"),),
        (
            want(
                "Quiz on unit two",
                UNCLEAR,
                words="Quiz on unit two\n\nSections 2 and 3",
                deadline=None,
                deadline_words=None,
                choices=(),
                named=(),
            ),
        ),
        id="no-deadline-from-the-school-day",
    ),
    pytest.param(
        (
            note(
                "Science",
                nov(3),
                "Online practice November 10th",
                "Lab notebook due on Wednesday",
            ),
        ),
        (
            want(
                "Online practice November 10th",
                UNCLEAR,
                words="Online practice November 10th",
                deadline=None,
                named=(nov(10),),
            ),
            want("Lab notebook", HOMEWORK, deadline=nov(4), named=()),
        ),
        id="a-named-date-is-not-a-deadline",
    ),
    pytest.param(
        (
            note(
                "Spanish",
                nov(3),
                "Study chapter five vocabulary for the oral quiz",
                "Due 10 de Nov.",
            ),
        ),
        (
            want(
                "Study chapter five vocabulary for the oral quiz",
                HOMEWORK,
                words="Study chapter five vocabulary for the oral quiz\n\nDue 10 de Nov.",
                deadline=None,
                deadline_words="Due 10 de Nov.",
                choices=(nov(10),),
            ),
        ),
        id="a-deadline-paragraph-dates-the-task-before-it",
    ),
    pytest.param(
        PRACTICE_WEEK[:1],
        (
            want(
                "Online practice",
                HOMEWORK,
                deadline=None,
                deadline_words="due Tuesday",
                choices=(nov(10), nov(17)),
            ),
        ),
        id="the-same-weekday-gives-two-choices",
    ),
    pytest.param(
        PRACTICE_WEEK,
        (
            want(
                "Online practice",
                HOMEWORK,
                deadline=None,
                choices=(nov(10), nov(17)),
                first_day=nov(10),
                seen_on=(nov(10), nov(11), nov(12), nov(13)),
                listed_after=(),
            ),
        ),
        id="one-line-listed-four-days-running",
    ),
    pytest.param(
        (note("Religion", nov(12), "Unit test today. No additional Religion homework assigned."),),
        (
            want(
                "Unit test today",
                NOT_ASSIGNMENT,
                words="Unit test today. No additional Religion homework assigned.",
                deadline=None,
                named=(nov(12),),
            ),
        ),
        id="no-homework-assigned",
    ),
    pytest.param(
        (
            *PRACTICE_WEEK[:2],
            note("Science", nov(12), "Reading log due today", PRACTICE),
            PRACTICE_WEEK[3],
        ),
        (
            want(
                "Online practice",
                HOMEWORK,
                deadline=None,
                choices=(nov(10), nov(17)),
                first_day=nov(10),
                seen_on=(nov(10), nov(11), nov(12), nov(13)),
            ),
            want("Reading log", HOMEWORK, deadline=nov(12), first_day=nov(12), seen_on=(nov(12),)),
        ),
        id="a-new-task-beside-one-seen-again",
    ),
    pytest.param(
        (
            note(
                "Spanish",
                nov(13),
                "Study and memorize the verb categories",
                "Be ready to form sentences with each category.",
            ),
        ),
        (
            want(
                "Study and memorize the verb categories",
                HOMEWORK,
                words=(
                    "Study and memorize the verb categories\n\n"
                    "Be ready to form sentences with each category."
                ),
                deadline=None,
                deadline_words=None,
            ),
        ),
        id="an-opening-word-with-its-supporting-text",
    ),
    pytest.param(
        (note("Humanities", nov(4), "Short essay Due."),),
        (
            want(
                "Short essay",
                HOMEWORK,
                deadline=None,
                deadline_words="Due",
                choices=(),
                named=(),
            ),
        ),
        id="due-alone-names-no-date",
    ),
    pytest.param(
        (note("Humanities", nov(16), "The class project is due today."),),
        (want("The class project", HOMEWORK, deadline=nov(16), deadline_words="due today"),),
        id="today-is-the-school-day",
    ),
    pytest.param(
        (
            note("Humanities", nov(2), "Map project due on Wednesday"),
            note("Humanities", nov(5), "Map project due on Wednesday"),
        ),
        (
            want(
                "Map project",
                HOMEWORK,
                deadline=nov(4),
                first_day=nov(2),
                seen_on=(nov(2), nov(5)),
                listed_after=(nov(5),),
            ),
        ),
        id="listed-after-its-deadline",
    ),
    pytest.param(
        (
            note("Science", nov(2), "Online practice due on Friday"),
            note("Science", nov(9), "Online practice due on Thursday"),
        ),
        (
            want("Online practice", HOMEWORK, deadline=nov(6), seen_on=(nov(2),)),
            want("Online practice", HOMEWORK, deadline=nov(12), seen_on=(nov(9),)),
        ),
        id="the-same-name-a-week-apart",
    ),
    pytest.param(
        (
            note("Science", nov(2), "Online practice due on Friday"),
            note("Science", nov(3), "Online practice, set B, due on Friday"),
        ),
        (
            want("Online practice", HOMEWORK, deadline=nov(6), seen_on=(nov(2),)),
            want(
                "Online practice, set B",
                HOMEWORK,
                deadline=None,
                choices=(nov(6),),
                seen_on=(nov(3),),
            ),
        ),
        id="the-same-name-in-different-words-the-next-day",
    ),
    pytest.param(
        (
            note("Science", nov(2), "Online practice due on Friday"),
            note("Art", nov(3), "Online practice due on Friday"),
        ),
        (
            want("Online practice", HOMEWORK, subject="Science", seen_on=(nov(2),)),
            want("Online practice", HOMEWORK, subject="Art", seen_on=(nov(3),)),
        ),
        id="the-same-words-in-another-subject",
    ),
    pytest.param(
        (note("Science", nov(10), PRACTICE), note("Science", nov(17), PRACTICE)),
        (
            want("Online practice", HOMEWORK, choices=(nov(10), nov(17)), seen_on=(nov(10),)),
            want("Online practice", HOMEWORK, choices=(nov(17), nov(24)), seen_on=(nov(17),)),
        ),
        id="the-same-words-a-full-week-later",
    ),
    pytest.param(
        (
            note("Spanish", nov(3), "Spelling packet due Friday."),
            note("Spanish", nov(9), "Spelling packet due Friday."),
        ),
        (
            want("Spelling packet", HOMEWORK, deadline=nov(6), seen_on=(nov(3),)),
            want("Spelling packet", HOMEWORK, deadline=nov(13), seen_on=(nov(9),)),
        ),
        id="the-same-words-in-the-next-school-week-six-days-later",
    ),
    pytest.param(
        (
            note("Science", nov(6), "Lab notebook due on Wednesday"),
            note("Science", nov(9), "Lab notebook due on Wednesday"),
        ),
        (
            want("Lab notebook", HOMEWORK, deadline=nov(11), seen_on=(nov(6),)),
            want("Lab notebook", HOMEWORK, deadline=nov(11), seen_on=(nov(9),)),
        ),
        id="a-friday-listing-then-the-next-monday",
    ),
    pytest.param(
        (
            note("Science", nov(2), "Lab notebook due on Friday"),
            note("Science", nov(6), "Lab notebook due on Friday"),
        ),
        (want("Lab notebook", HOMEWORK, deadline=nov(6), seen_on=(nov(2), nov(6))),),
        id="the-same-words-monday-to-friday-of-one-school-week",
    ),
    pytest.param(
        (
            note(
                "Science",
                nov(3),
                "Lab noteb00k du3 on Wednesday",
                "Online practice due Friday",
                suspect=True,
            ),
        ),
        (
            want(
                "Lab noteb00k du3 on Wednesday",
                UNCLEAR,
                words="Lab noteb00k du3 on Wednesday\n\nOnline practice due Friday",
                deadline=None,
                deadline_words=None,
                choices=(),
                named=(),
            ),
        ),
        id="a-suspect-block-is-one-unclear-task",
    ),
    pytest.param(
        (note("Math", nov(2), "Worksheet due tomorrow"),),
        (want("Worksheet", HOMEWORK, deadline=nov(3), deadline_words="due tomorrow"),),
        id="edge-tomorrow-is-the-day-after-the-school-day",
    ),
    pytest.param(
        (note("Art", nov(2), "Poster due Wednesday 11/05"),),
        (
            want(
                "Poster",
                HOMEWORK,
                deadline=None,
                deadline_words="due Wednesday 11/05",
                choices=(nov(4), nov(5)),
            ),
        ),
        id="edge-a-weekday-and-a-date-that-disagree",
    ),
    pytest.param(
        (note("Art", nov(2), "Please bring a shoebox by next Tuesday"),),
        (
            want(
                "Please bring a shoebox",
                HOMEWORK,
                deadline=None,
                deadline_words="by next Tuesday",
                choices=(nov(3), nov(10)),
            ),
        ),
        id="edge-next-weekday-alone-gives-two-choices",
    ),
    pytest.param(
        (note("Humanities", nov(2), "Read the poem written by the class"),),
        (
            want(
                "Read the poem written by the class",
                UNCLEAR,
                deadline=None,
                deadline_words=None,
                choices=(),
            ),
        ),
        id="edge-by-with-no-date-is-not-a-deadline",
    ),
    pytest.param(
        (note("Humanities", nov(2), f"{LONG_WORDS} due on Friday"),),
        (
            want(
                " ".join(["abcdefghi"] * 20),
                HOMEWORK,
                words=f"{LONG_WORDS} due on Friday",
                deadline=nov(6),
            ),
        ),
        id="edge-a-long-name-is-cut-at-a-word",
    ),
    pytest.param(
        (note("Humanities", nov(2), LONG_WORDS),),
        (want(" ".join(["abcdefghi"] * 20), UNCLEAR, words=LONG_WORDS),),
        id="edge-a-long-first-line-is-cut-at-a-word",
    ),
    pytest.param(
        (note("Art", nov(2), "Bring colored pencils, glue, etc. Poster due on Friday."),),
        (
            want(
                "Bring colored pencils, glue, etc. Poster",
                UNCLEAR,
                words="Bring colored pencils, glue, etc. Poster due on Friday.",
                deadline=None,
                deadline_words="due on Friday",
                choices=(nov(6),),
            ),
        ),
        id="edge-an-unsure-split-keeps-the-paragraph-whole",
    ),
    pytest.param(
        (note("Art", nov(2), "Draft due Wednesday and final copy due Friday."),),
        (
            want(
                "Draft",
                UNCLEAR,
                words="Draft due Wednesday and final copy due Friday.",
                deadline=None,
                deadline_words=None,
                choices=(),
            ),
        ),
        id="edge-two-deadlines-in-one-sentence-settle-none",
    ),
    pytest.param(
        (note("Math", nov(2), "Worksheet due by Friday"),),
        (want("Worksheet", HOMEWORK, deadline=nov(6), deadline_words="due by Friday"),),
        id="edge-due-by-is-one-statement",
    ),
    pytest.param(
        (note("Math", nov(2), "No homework due tomorrow"),),
        (want("No homework", NOT_ASSIGNMENT),),
        id="edge-a-name-that-says-no-homework",
    ),
    pytest.param(
        (note("Humanities", nov(2), "Map project due on Wednesday", "Use the rubric from class."),),
        (
            want(
                "Map project",
                HOMEWORK,
                words="Map project due on Wednesday\n\nUse the rubric from class.",
                deadline=None,
                choices=(nov(4),),
            ),
        ),
        id="edge-text-with-no-deadline-stays-with-the-task-before",
    ),
    pytest.param(
        (note("Humanities", nov(5), "Vocabulary quiz on Thursday"),),
        (want("Vocabulary quiz on Thursday", UNCLEAR, deadline=None, named=()),),
        id="edge-a-named-weekday-that-could-be-two-days-names-none",
    ),
    pytest.param(
        (note("Math", nov(2), "Test coming up on 11/12"),),
        (want("Test coming up on 11/12", UNCLEAR, deadline=None, named=(nov(12),)),),
        id="edge-a-numeric-date-outside-a-deadline-is-named",
    ),
    pytest.param(
        (
            note("Humanities", nov(3), "Map project due on Wednesday"),
            note("Humanities", nov(2), "Map project due on Wednesday"),
        ),
        (
            want(
                "Map project",
                HOMEWORK,
                deadline=nov(4),
                first_day=nov(2),
                seen_on=(nov(2), nov(3)),
            ),
        ),
        id="edge-the-first-day-is-the-earliest-day-listed",
    ),
    pytest.param(
        (note("Math", nov(2), "Worksheet due 11/31"),),
        (
            want(
                "Worksheet due 11/31",
                UNCLEAR,
                deadline=None,
                deadline_words=None,
                choices=(),
                named=(),
            ),
        ),
        id="edge-a-day-the-month-does-not-have-is-no-date",
    ),
    pytest.param(
        (note("Humanities", nov(2), "Map project due Wednesday\nTimeline poster due Thursday"),),
        (
            want(
                "Map project",
                UNCLEAR,
                words="Map project due Wednesday\nTimeline poster due Thursday",
                deadline=None,
                deadline_words=None,
                choices=(),
            ),
        ),
        id="kept-whole-two-deadline-lines-in-one-paragraph",
    ),
    pytest.param(
        (note("Math", nov(2), "Due Friday"),),
        (want("Due Friday", UNCLEAR, deadline=nov(6), deadline_words="Due Friday"),),
        id="kept-whole-a-deadline-with-nothing-before-it",
    ),
    pytest.param(
        (note("Math", nov(2), "Study for the quiz. No additional homework assigned."),),
        (
            want(
                "Study for the quiz",
                UNCLEAR,
                words="Study for the quiz. No additional homework assigned.",
                deadline=None,
            ),
        ),
        id="kept-whole-an-opening-word-beside-no-homework",
    ),
    pytest.param(
        (note("Art", nov(2), "Poster due on November 5th"),),
        (
            want(
                "Poster",
                HOMEWORK,
                deadline=nov(5),
                deadline_words="due on November 5th",
                choices=(),
                named=(),
            ),
        ),
        id="on-before-a-month-and-day-in-a-deadline",
    ),
    pytest.param(
        (note("Art", nov(2), "Poster due on 11/05"),),
        (
            want(
                "Poster",
                HOMEWORK,
                deadline=nov(5),
                deadline_words="due on 11/05",
                choices=(),
                named=(),
            ),
        ),
        id="on-before-a-numeric-date-in-a-deadline",
    ),
    pytest.param(
        (note("Art", nov(2), "Shoebox by on Wednesday"),),
        (
            want(
                "Shoebox by on Wednesday",
                UNCLEAR,
                deadline=None,
                deadline_words=None,
                choices=(),
                named=(nov(4),),
            ),
        ),
        id="by-then-on-is-not-a-deadline",
    ),
    pytest.param(
        (note("Math", nov(2), "Worksheet due 11/10/2026"),),
        (
            want(
                "Worksheet",
                HOMEWORK,
                deadline=nov(10),
                deadline_words="due 11/10/2026",
                choices=(),
                named=(),
            ),
        ),
        id="a-numeric-date-with-its-year",
    ),
    pytest.param(
        (note("Art", nov(2), "Poster due November 5th, 2026"),),
        (
            want(
                "Poster",
                HOMEWORK,
                deadline=nov(5),
                deadline_words="due November 5th, 2026",
                choices=(),
                named=(),
            ),
        ),
        id="a-month-and-day-with-its-year",
    ),
    pytest.param(
        (note("Math", nov(2), "Packet due 1/5/2027"),),
        (
            want(
                "Packet",
                HOMEWORK,
                deadline=date(2027, 1, 5),
                deadline_words="due 1/5/2027",
                choices=(),
                named=(),
            ),
        ),
        id="a-date-with-another-year-is-read-in-that-year",
    ),
    pytest.param(
        (note("Humanities", nov(2), "Class is canceled due to the assembly."),),
        (
            want(
                "Class is canceled due to the assembly",
                UNCLEAR,
                words="Class is canceled due to the assembly.",
                deadline=None,
                deadline_words=None,
                choices=(),
            ),
        ),
        id="due-before-other-words-is-not-a-deadline",
    ),
    pytest.param(
        (note("Art", nov(2), "Poster due next class"),),
        (
            want(
                "Poster due next class",
                UNCLEAR,
                words="Poster due next class",
                deadline=None,
                deadline_words=None,
                choices=(),
            ),
        ),
        id="due-before-next-class-is-not-a-deadline",
    ),
    pytest.param(
        (note("Geometry", nov(4), PACKET),),
        (
            want(
                "Review packet",
                HOMEWORK,
                words=PACKET,
                deadline=None,
                deadline_words="due 11/10",
                choices=(nov(10),),
                named=(nov(5),),
            ),
        ),
        id="a-deadline-then-a-named-day-in-one-sentence",
    ),
    pytest.param(
        (note("Science", nov(4), "Online practice November 10th- B2"),),
        (
            want(
                "Online practice November 10th- B2",
                UNCLEAR,
                deadline=None,
                deadline_words=None,
                named=(nov(10),),
            ),
        ),
        id="a-named-date-with-a-code-after-it",
    ),
    pytest.param(
        (
            note(
                "Geometry",
                nov(13),
                CLASS_SITE,
                "Math practice - Reflections and Translations due Monday 11/16",
            ),
        ),
        (
            want(CLASS_SITE, HOMEWORK, words=CLASS_SITE, deadline=None, deadline_words=None),
            want(
                "Math practice - Reflections and Translations",
                HOMEWORK,
                deadline=None,
                deadline_words="due Monday 11/16",
                choices=(nov(16),),
            ),
        ),
        id="an-opening-word-then-a-dated-task",
    ),
    pytest.param(
        (note("Religion", nov(5), f"{SLIDES} Essay project Due."),),
        (
            want(
                SLIDES.removesuffix("."),
                HOMEWORK,
                words=SLIDES,
                deadline=None,
                deadline_words=None,
                named=(nov(12),),
            ),
            want(
                "Essay project",
                HOMEWORK,
                words="Essay project Due.",
                deadline=None,
                deadline_words="Due",
                choices=(),
            ),
        ),
        id="an-opening-sentence-then-due-alone",
    ),
    pytest.param(
        (note("Geometry", nov(11), *CLASS_TIME),),
        (
            want(
                CLASS_TIME[0].removesuffix("."),
                UNCLEAR,
                words="\n\n".join(CLASS_TIME),
                deadline=None,
                deadline_words=None,
                named=(),
            ),
        ),
        id="an-opening-word-in-supporting-text-does-not-count",
    ),
    pytest.param(
        (note("Religion", nov(11), LAST_DAY),),
        (
            want(
                "Final day to review for tomorrow's unit assessment",
                UNCLEAR,
                words=LAST_DAY,
                deadline=None,
                deadline_words=None,
                named=(nov(11), nov(12)),
            ),
        ),
        id="today-and-tomorrow-named-in-day-order",
    ),
    pytest.param(
        (note("Humanities", nov(2), "Map project. Due Wednesday. Timeline poster due Thursday."),),
        (
            want(
                "Map project",
                HOMEWORK,
                words="Map project. Due Wednesday.",
                deadline=None,
                deadline_words="Due Wednesday",
                choices=(nov(4),),
            ),
            want("Timeline poster", HOMEWORK, deadline=nov(5)),
        ),
        id="a-deadline-sentence-dates-the-task-before-it",
    ),
    pytest.param(
        (note("Art", nov(2), "Poster due Nov. 3. Bring glue."),),
        (
            want(
                "Poster",
                HOMEWORK,
                words="Poster due Nov. 3. Bring glue.",
                deadline=None,
                deadline_words="due Nov. 3",
                choices=(nov(3),),
            ),
        ),
        id="a-period-inside-a-date-is-not-a-sentence-end",
    ),
    pytest.param(
        (note("Science", nov(2), PRACTICE), note("Science", nov(2), PRACTICE)),
        (want("Online practice", HOMEWORK, deadline=nov(3), seen_on=(nov(2),)),),
        id="the-same-words-twice-under-one-day",
    ),
    pytest.param(
        (note("Science", nov(2), "Study hall is moved to room 12 on Friday."),),
        (want("Study hall is moved to room 12 on Friday", UNCLEAR, deadline=None),),
        id="an-opening-word-as-a-noun-before-is",
    ),
    pytest.param(
        (note("Science", nov(2), "Use of phones is not allowed during the quiz."),),
        (want("Use of phones is not allowed during the quiz", UNCLEAR, deadline=None),),
        id="an-opening-word-before-of",
    ),
    pytest.param(
        (note("Science", nov(2), "Review of unit 2 went well today."),),
        (want("Review of unit 2 went well today", UNCLEAR, deadline=None),),
        id="an-opening-word-before-of-and-went",
    ),
    pytest.param(
        (note("Science", nov(2), "Study for the quiz on Friday."),),
        (want("Study for the quiz on Friday", HOMEWORK, deadline=None),),
        id="an-opening-word-as-an-instruction-to-study",
    ),
    pytest.param(
        (note("Science", nov(2), "Use the study guide to review unit 2."),),
        (want("Use the study guide to review unit 2", HOMEWORK, deadline=None),),
        id="an-opening-word-as-an-instruction-to-use",
    ),
    pytest.param(
        (note("Science", nov(2), "Review chapter 3 and bring the worksheet."),),
        (want("Review chapter 3 and bring the worksheet", HOMEWORK, deadline=None),),
        id="an-opening-word-as-an-instruction-to-review",
    ),
    pytest.param(
        (note("Math", nov(2), "No homework tonight, but study for the quiz due Friday."),),
        (
            want(
                "No homework tonight, but study for the quiz",
                UNCLEAR,
                deadline=nov(6),
                deadline_words="due Friday",
            ),
        ),
        id="no-homework-beside-a-deadline-of-other-words",
    ),
    pytest.param(
        (note("Math", nov(2), "No homework tonight, but study for the quiz."),),
        (want("No homework tonight, but study for the quiz", UNCLEAR, deadline=None),),
        id="no-homework-beside-an-opening-word-after-it",
    ),
    pytest.param(
        (note("Math", nov(2), "No additional homework assigned."),),
        (want("No additional homework assigned", NOT_ASSIGNMENT, deadline=None),),
        id="no-homework-alone",
    ),
    pytest.param((), (), id="edge-no-notes-no-tasks"),
]


@pytest.mark.parametrize(("blocks", "wants"), ROWS)
def test_notes_read_as_their_tasks(blocks: tuple[NoteBlock, ...], wants: tuple[Want, ...]) -> None:
    tasks = read_notes(blocks)

    assert [task.name for task in tasks] == [wanted["name"] for wanted in wants]
    for task, wanted in zip(tasks, wants, strict=True):
        assert {field: getattr(task, field) for field in wanted} == wanted


@pytest.mark.parametrize(("blocks", "wants"), ROWS)
def test_every_task_keeps_its_days_in_order_and_its_name_short(
    blocks: tuple[NoteBlock, ...], wants: tuple[Want, ...]
) -> None:
    tasks = read_notes(blocks)

    assert len(tasks) == len(wants)
    for task in tasks:
        assert isinstance(task, NoteTask)
        assert task.seen_on == tuple(sorted(set(task.seen_on)))
        assert task.first_day == task.seen_on[0]
        assert 0 < len(task.name) <= NAME_LIMIT
        assert task.deadline is None or not task.choices
        assert task.deadline is not None or not task.listed_after


def test_a_task_carries_the_line_of_the_note_it_was_first_listed_under() -> None:
    first = NoteBlock("Science", nov(2), ("Lab notebook due on Wednesday",), line=14)
    again = NoteBlock("Science", nov(3), ("Lab notebook due on Wednesday",), line=31)

    tasks = read_notes((first, again))

    assert [task.line for task in tasks] == [14]


DECEMBER_15 = date(2026, 12, 15)
"""A Tuesday in December 2026; January 8, 2027 is a Friday, January 8, 2026 a Thursday."""

DECEMBER_TO_JANUARY = [
    pytest.param(DECEMBER_15, "Essay due January 8", date(2027, 1, 8), id="english"),
    pytest.param(DECEMBER_15, "Essay due 1/8", date(2027, 1, 8), id="numeric"),
    pytest.param(DECEMBER_15, "Essay due 8 de enero", date(2027, 1, 8), id="spanish"),
    pytest.param(DECEMBER_15, "Essay due 8 de ene.", date(2027, 1, 8), id="spanish-short"),
    pytest.param(
        DECEMBER_15, "Essay due 8 de enero de 2026", date(2026, 1, 8), id="spanish-year-written"
    ),
    pytest.param(
        DECEMBER_15, "Essay due 8 de enero de 2027", date(2027, 1, 8), id="spanish-next-year"
    ),
    pytest.param(
        DECEMBER_15, "Essay due January 8, 2026", date(2026, 1, 8), id="english-year-written"
    ),
    pytest.param(DECEMBER_15, "Essay due 1/8/2026", date(2026, 1, 8), id="numeric-year-written"),
    pytest.param(
        DECEMBER_15, "Essay due 8 de enero del 2026", date(2026, 1, 8), id="spanish-del-year"
    ),
    pytest.param(
        DECEMBER_15, "Essay due 8 de enero, 2026", date(2026, 1, 8), id="spanish-comma-year"
    ),
    pytest.param(
        DECEMBER_15, "Essay due Friday, January 8", date(2027, 1, 8), id="weekday-beside-date"
    ),
    pytest.param(
        DECEMBER_15,
        "Essay due January 8, 1500 words.",
        date(2027, 1, 8),
        id="english-count-no-year",
    ),
    pytest.param(
        DECEMBER_15,
        "Essay due 8 de enero, 1500 palabras.",
        date(2027, 1, 8),
        id="spanish-count-no-year",
    ),
    pytest.param(
        DECEMBER_15, "Essay due January 8 1500 words.", date(2027, 1, 8), id="english-space-count"
    ),
    pytest.param(
        DECEMBER_15, "Essay due January 8, 1000 points.", date(2027, 1, 8), id="english-points"
    ),
    pytest.param(
        DECEMBER_15,
        "Essay due January 8, 2028 at noon.",
        date(2028, 1, 8),
        id="english-year-then-prose",
    ),
    pytest.param(
        DECEMBER_15, "Essay due January 8, 2028.", date(2028, 1, 8), id="english-far-year-written"
    ),
    pytest.param(
        DECEMBER_15, "Essay due January 8,2028.", date(2028, 1, 8), id="english-comma-no-space-year"
    ),
    pytest.param(
        DECEMBER_15, "Essay due Jan. 8,2028", date(2028, 1, 8), id="english-short-comma-no-space"
    ),
    pytest.param(
        DECEMBER_15,
        "Essay due January 8,1500 words.",
        date(2027, 1, 8),
        id="english-comma-no-space-count",
    ),
    pytest.param(DECEMBER_15, "Essay due 1/8/2028.", date(2028, 1, 8), id="numeric-far-year"),
    pytest.param(DECEMBER_15, "Essay due 1/8/1500.", date(1500, 1, 8), id="numeric-always-a-year"),
    pytest.param(
        DECEMBER_15, "Essay due 8 de enero de 2028.", date(2028, 1, 8), id="spanish-de-far-year"
    ),
    pytest.param(
        DECEMBER_15, "Essay due 8 de enero del 2028.", date(2028, 1, 8), id="spanish-del-far-year"
    ),
    pytest.param(DECEMBER_15, "Essay due December 18", date(2026, 12, 18), id="december-stays"),
    pytest.param(DECEMBER_15, "Essay due February 3", date(2026, 2, 3), id="february-stays"),
    pytest.param(nov(16), "Essay due January 8", date(2026, 1, 8), id="november-school-day"),
]


@pytest.mark.parametrize(("day", "words", "deadline"), DECEMBER_TO_JANUARY)
def test_a_january_date_read_under_a_december_school_day_is_in_the_following_year(
    day: date, words: str, deadline: date
) -> None:
    """A month and day in January with no year, read under a school day in December, is in
    the following year, written in English, as numbers or in Spanish; a year written with it
    wins, however far from the school day, and no other month or school day moves. A
    four-digit number after a comma or a space with a counting noun right after it counts
    something ("1500 words") and is no year; with other words after it ("at noon"), "de" or
    "del" before it, or in the numeric form, it is the year. A weekday beside such a date
    that falls on it in the following year gives that one date. The reader checks only that
    the date is on the calendar."""
    (task,) = read_notes((note("Language Arts", day, words),))

    assert (task.name, task.standing) == ("Essay", HOMEWORK)
    assert (task.deadline, task.choices) == (deadline, ())


MIXED = "No homework due tomorrow, but the essay is due Friday."
"""A note that says no homework is due one day and sets a deadline for another day."""


def test_a_no_homework_statement_followed_by_a_deadline_is_unclear_with_that_date_named() -> None:
    """Words that say no homework is due one day and then set a deadline are unclear, never
    "no homework", and the date of that later deadline is named for review."""
    (task,) = read_notes((note("Language Arts", nov(2), MIXED),))

    assert task.standing is UNCLEAR
    assert (task.deadline, task.choices) == (None, ())
    assert task.named == (nov(6),)


MONDAY_16 = nov(16)
"""A Monday; that week's Wednesday, Thursday and Friday are November 18, 19 and 20."""

NEGATED = [
    ("Essay is not due Friday; finish it Monday", (nov(16), nov(23))),
    ("Essay is not due Friday; finish it Monday.", (nov(16), nov(23))),
    ("Essay isn't due Friday.", ()),
    ("Essay will not be due Friday.", ()),
    ("Essay is no " + "longer due Friday.", ()),
]


@pytest.mark.parametrize(("words", "named"), NEGATED)
def test_a_deadline_cue_negated_right_before_it_settles_nothing(
    words: str, named: tuple[date, ...]
) -> None:
    """A negation, "not", "never", "no" then "longer", or a word ending in "n't", right before "due"
    or "by", with linking words between, makes the statement settle nothing: no deadline, no
    choice and no date named from it; a weekday named elsewhere is named as every date it could
    mean, and the words stay as written."""
    (task,) = read_notes((note("Language Arts", MONDAY_16, words),))

    assert (task.deadline, task.deadline_words) == (None, None)
    assert (task.choices, task.named) == ((), named)
    assert task.words == words


@pytest.mark.parametrize("words", ["Do not forget: essay due Friday.", "Essay due Friday."])
def test_a_negation_not_right_before_the_cue_leaves_the_deadline(words: str) -> None:
    """A negation earlier in the words, not right before the cue, negates nothing."""
    (task,) = read_notes((note("Language Arts", MONDAY_16, words),))

    assert (task.deadline, task.choices) == (nov(20), ())


ALTERNATIVES = [
    "Essay due Wednesday or Thursday.",
    "Essay due Wednesday, or Thursday.",
    "Essay due November 18 or November 19",
    "Essay due November 18, or November 19",
]


@pytest.mark.parametrize("words", ALTERNATIVES)
def test_two_dates_joined_by_or_after_a_cue_settle_nothing_and_are_both_choices(
    words: str,
) -> None:
    """A date expression after a cue, then "or" (or ", or") and a second date expression,
    is one statement whose dates are every date either could mean: no deadline, both dates
    its choices, none named."""
    (task,) = read_notes((note("Language Arts", MONDAY_16, words),))

    assert (task.name, task.standing) == ("Essay", HOMEWORK)
    assert task.deadline is None
    assert (task.choices, task.named) == ((nov(18), nov(19)), ())


@pytest.mark.parametrize(
    ("words", "deadline"),
    [
        ("Essay due January 8, 1500\nwords", date(2027, 1, 8)),
        ("Essay due January 8, 1500\nwords.", date(2027, 1, 8)),
    ],
    ids=["count", "count-period"],
)
def test_four_digits_with_a_counting_noun_on_the_next_line_are_a_count(
    words: str, deadline: date
) -> None:
    """Four digits after a date with a counting noun after a line break count something, as
    on one line."""
    (task,) = read_notes((note("Language Arts", DECEMBER_15, words),))

    assert (task.deadline, task.choices) == (deadline, ())


@pytest.mark.parametrize(
    "words",
    [
        "Essay due 8 de enero de 2028, traigan el libro.",
        "Essay due 8 de enero, 2028 traigan el libro.",
        "Essay due January 8, 2028\nplease",
    ],
    ids=["spanish-year-then-prose", "spanish-comma-year-then-prose", "year-then-prose"],
)
def test_a_written_year_before_a_trailing_instruction_is_offered_unselected(words: str) -> None:
    """Four digits after a date with other words after them, on the same line or the next,
    are the year; the words after the date leave the deadline unset, and the date with its
    written year is offered unselected."""
    (task,) = read_notes((note("Language Arts", DECEMBER_15, words),))

    assert (task.name, task.standing) == ("Essay", HOMEWORK)
    assert (task.deadline, task.choices) == (None, (date(2028, 1, 8),))


@pytest.mark.parametrize(
    "words",
    [
        "Draft due Wednesday and final copy due Friday",
        "Draft due Wednesday and final copy due Friday.",
    ],
    ids=["no-period", "period"],
)
def test_several_deadline_statements_keep_each_date_with_its_milestone(words: str) -> None:
    """Two deadline statements in one task, each settling one date: no single deadline, and
    each date kept with the words its clause names before its cue, without joining words or
    punctuation. A milestone's date is never the deadline, a choice or a named date."""
    (task,) = read_notes((note("Art", MONDAY_16, words),))

    assert (task.name, task.words) == ("Draft", words)
    assert (task.deadline, task.choices, task.named) == (None, (), ())
    assert task.milestones == (Milestone("Draft", nov(18)), Milestone("final copy", nov(20)))


def test_a_milestone_clause_drops_then_and_a_statement_settling_no_one_date_keeps_none() -> None:
    """A clause's opening "then" and punctuation are no part of its milestone; a statement
    whose weekday the school day shares keeps both dates it could mean with its label."""
    then_words = "Draft due Wednesday, then final copy due Friday."
    (then,) = read_notes((note("Art", MONDAY_16, then_words),))
    (open_,) = read_notes((note("Art", MONDAY_16, "Draft due Monday and final copy due Friday."),))

    assert then.milestones == (Milestone("Draft", nov(18)), Milestone("final copy", nov(20)))
    assert open_.milestones == (
        Milestone("Draft", nov(16)),
        Milestone("Draft", nov(23)),
        Milestone("final copy", nov(20)),
    )
    assert open_.deadline is None


def test_a_no_homework_statement_with_a_later_deadline_keeps_no_milestone() -> None:
    """Words that say no homework is due one day and then set a deadline name that date for
    review and keep no milestone."""
    (task,) = read_notes((note("Language Arts", nov(2), MIXED),))

    assert task.named == (nov(6),)
    assert task.milestones == ()


@pytest.mark.parametrize(
    ("words", "ascii_words"),
    [
        ("Essay isn\N{RIGHT SINGLE QUOTATION MARK}t due Friday.", "Essay isn't due Friday."),
        ("Essay won\N{RIGHT SINGLE QUOTATION MARK}t be due Friday.", "Essay won't be due Friday."),
    ],
    ids=["isnt", "wont-be"],
)
def test_a_word_ending_in_n_t_with_a_typographic_apostrophe_negates_the_cue(
    words: str, ascii_words: str
) -> None:
    """A word ending in "n't" written with a typographic apostrophe negates the cue right
    after it as the same word with an ASCII apostrophe does: no deadline, no choice and no
    date named from that statement; the words stay as written."""
    (task,) = read_notes((note("Language Arts", MONDAY_16, words),))
    (ascii_task,) = read_notes((note("Language Arts", MONDAY_16, ascii_words),))

    read = (task.deadline, task.deadline_words, task.choices, task.named)
    assert read == (None, None, (), ())
    assert read == (
        ascii_task.deadline,
        ascii_task.deadline_words,
        ascii_task.choices,
        ascii_task.named,
    )
    assert task.words == words


FRIDAY_13 = nov(13)
"""A Friday; the next Monday, Tuesday and Wednesday are November 16, 17 and 18."""


@pytest.mark.parametrize(
    ("words", "choices"),
    [
        ("Essay due Monday, Tuesday or Wednesday.", (nov(16), nov(17), nov(18))),
        ("Essay due Monday, Tuesday, or Wednesday.", (nov(16), nov(17), nov(18))),
        ("Essay due Monday or Tuesday or Wednesday.", (nov(16), nov(17), nov(18))),
        ("Essay due November 18, November 19 or November 20", (nov(18), nov(19), nov(20))),
    ],
    ids=["comma-or", "comma-comma-or", "or-or", "dates-comma-or"],
)
def test_dates_listed_after_a_cue_and_joined_by_a_final_or_are_all_choices(
    words: str, choices: tuple[date, ...]
) -> None:
    """Date expressions listed after a cue, separated by commas or "or" and joined by a final
    "or", are one statement that settles nothing: no deadline, every date any of them could
    mean a choice, in order and once, and none named."""
    (task,) = read_notes((note("Language Arts", FRIDAY_13, words),))

    assert (task.name, task.standing) == ("Essay", HOMEWORK)
    assert (task.deadline, task.deadline_words) == (None, words.rstrip(".")[len("Essay ") :])
    assert (task.choices, task.named) == (choices, ())


@pytest.mark.parametrize(
    ("words", "choices"),
    [
        ("Essay due Monday, bring your notes.", (nov(16),)),
        ("Essay due Monday, Tuesday.", (nov(16), nov(17))),
        ("Essay due Monday, Tuesday and Wednesday.", (nov(16), nov(17), nov(18))),
    ],
    ids=["comma-words", "comma-date", "comma-and"],
)
def test_dates_listed_after_a_cue_with_no_or_leave_the_deadline_unset(
    words: str, choices: tuple[date, ...]
) -> None:
    """Words after a deadline's date in its sentence, and dates joined to it by a comma or
    "and", leave the deadline unset: the cue's date and each joined date are choices."""
    (task,) = read_notes((note("Language Arts", FRIDAY_13, words),))

    assert (task.deadline, task.choices, task.named) == (None, choices, ())


def test_two_milestones_that_share_a_date_are_both_kept() -> None:
    """Two deadline statements settling the same date each keep their milestone, and the
    date is no deadline, choice or named date."""
    (task,) = read_notes((note("Art", MONDAY_16, "Draft due Friday and final copy due Friday."),))

    assert task.milestones == (Milestone("Draft", nov(20)), Milestone("final copy", nov(20)))
    assert (task.deadline, task.choices, task.named) == (None, (), ())


TUESDAY_17 = nov(17)
APRIL_26 = date(2027, 4, 26)


def read_one(words: str, day: date = MONDAY_16) -> tuple[NoteTask, ...]:
    """The tasks of one note holding the words, listed under the school day."""
    return read_notes((note("English", day, words),))


def offered(task: NoteTask) -> tuple[date, ...]:
    """Every date a task offers unselected: its choices and its named dates, each once."""
    return tuple(sorted({*task.choices, *task.named}))


SETS = [
    ("Essay due Friday.", MONDAY_16, nov(20)),
    ("Don't forget: essay due Friday.", MONDAY_16, nov(20)),
    ("Don't forget the essay is due Friday.", MONDAY_16, nov(20)),
    ("Don't forget it's due Friday.", MONDAY_16, nov(20)),
    ("Worksheet No. 3 due Friday.", MONDAY_16, nov(20)),
    ("Essay due Friday, not Thursday.", MONDAY_16, nov(20)),
    ("Read chapter 5 and answer questions 1-3, due Friday.", MONDAY_16, nov(20)),
    ("Lab report due Friday by 3pm.", MONDAY_16, nov(20)),
    ("Essay due Friday, November 20.", MONDAY_16, nov(20)),
    ("Essay due January 8 1500 words.", DECEMBER_15, date(2027, 1, 8)),
    ("Essay due January 8, 1500\nwords.", DECEMBER_15, date(2027, 1, 8)),
    ("Quiz due Friday, 20 questions.", MONDAY_16, nov(20)),
    ("Extended response due Friday.", MONDAY_16, nov(20)),
    ("Reply due Friday.", MONDAY_16, nov(20)),
    ("Assembly due Friday.", MONDAY_16, nov(20)),
    ("Complete the problems without a calculator by Friday.", MONDAY_16, nov(20)),
    ("Project due May 5.", APRIL_26, date(2027, 5, 5)),
    ("Essay now due Monday, November 23.", MONDAY_16, nov(23)),
    ("Essay was due Friday, now due Monday.", TUESDAY_17, nov(23)),
    ("The essay was due Friday and is now due Monday.", TUESDAY_17, nov(23)),
    ("Essay originally due Friday is now due Monday.", TUESDAY_17, nov(23)),
    ("Optional essay due Friday.", MONDAY_16, nov(20)),
    ("Reminder: essay due Friday.", MONDAY_16, nov(20)),
    ("Bring a family photo by Friday.", MONDAY_16, nov(20)),
    ("Read the Early Republic chapter by Friday.", MONDAY_16, nov(20)),
    ("Essay for some students due Friday.", MONDAY_16, nov(20)),
    ("Essay will be due Friday.", MONDAY_16, nov(20)),
    ("Essay previous" + "ly due Friday, now due Monday.", TUESDAY_17, nov(23)),
    ("Multiply the fractions by Friday.", MONDAY_16, nov(20)),
]


@pytest.mark.parametrize(("words", "day", "deadline"), SETS)
def test_one_complete_affirmative_sentence_sets_the_school_deadline(
    words: str, day: date, deadline: date
) -> None:
    """A task that is one recognized sentence with one date sets that date as the school's
    deadline and offers nothing else."""
    (task,) = read_one(words, day)

    assert (task.deadline, task.choices, task.named, task.milestones) == (deadline, (), (), ())


UNSET = [
    ("Essay due Friday. Write 500 words about your summer.", MONDAY_16, (20,)),
    ("Essay due Friday. Don't forget to cite your sources.", MONDAY_16, (20,)),
    ("Essay due Friday. Scratch that, more info soon.", MONDAY_16, (20,)),
    ("Essay due Friday. Optional for most students.", MONDAY_16, (20,)),
    (
        "Essay due Wednesday; anyone who doesn't finish can turn it in Thursday.",
        MONDAY_16,
        (18, 19),
    ),
    ("Essay due Friday is optional.", MONDAY_16, (20,)),
    ("Essay, optional for most, due Friday.", MONDAY_16, (20,)),
    ("Essay, not a poem, due Friday.", MONDAY_16, (20,)),
    ("Essay due Friday - cancel" + "led.", MONDAY_16, (20,)),
    ("Essay due Friday (cancel" + "led).", MONDAY_16, (20,)),
    ("Reading log due Friday: canceled this week.", MONDAY_16, (20,)),
    ("Essay due Friday. Postponed.", MONDAY_16, (20,)),
    ("Essay due Friday. Update: extended!", MONDAY_16, (20,)),
    ("Essay due Friday?", MONDAY_16, (20,)),
    ("Is the essay due Friday?", MONDAY_16, (20,)),
    ("Essay due Friday, right?", MONDAY_16, (20,)),
    ("Read pp. 5-10. Essay due Friday. Questions?", MONDAY_16, (20,)),
    ("Essay due Friday, not this week.", MONDAY_16, (20,)),
    ("Essay due Friday, not this Friday but next.", MONDAY_16, (20,)),
    ("Essay due Wednesday, if not Thursday.", MONDAY_16, (18, 19)),
    ("Essay due November 20, 2 pages double spaced.", MONDAY_16, (20,)),
    ("Essay due Friday in class.", MONDAY_16, (20,)),
    ("Essay due Friday. No late work accepted.", MONDAY_16, (20,)),
    ("Essay due November 18 or 19.", MONDAY_16, (18, 19)),
    ("Essay due November 18th or 19th.", MONDAY_16, (18, 19)),
    ("Presentations due Nov. 18 & 19.", MONDAY_16, (18, 19)),
    ("Essay due Nov 18\N{EN DASH}19.", MONDAY_16, (18, 19)),
    ("Presentations due November 18 through 20.", MONDAY_16, (18, 20)),
    ("Essay due Wednesday or Thurs.", MONDAY_16, (18, 19)),
    ("Essay due Wednesday (or Thursday).", MONDAY_16, (18, 19)),
    ("Essay due Wednesday/Thursday.", MONDAY_16, (18, 19)),
    ("Essay due Wednesday or on Thursday.", MONDAY_16, (18, 19)),
    ("Essay due Wednesday; or Thursday.", MONDAY_16, (18, 19)),
    ("Essay due Wednesday - or Thursday.", MONDAY_16, (18, 19)),
    ("Essay due 11/18 or 19.", MONDAY_16, (18, 19)),
    ("Essay due Monday, Tuesday and Wednesday.", FRIDAY_13, (16, 17, 18)),
    ("Essay due Wednesday, Thursday.", MONDAY_16, (18, 19)),
    ("The essay due Friday is no " + "longer required.", MONDAY_16, (20,)),
    ("Reading log due Friday won't be collected this week.", MONDAY_16, (20,)),
    ("Essay due Friday. Update: it has been moved to Monday.", TUESDAY_17, (20, 23)),
    ("Essay due Friday. Not anymore, see the new date.", MONDAY_16, (20,)),
    ("Essay might be due Friday.", MONDAY_16, (20,)),
    ("Essay is tentatively due Friday.", MONDAY_16, (20,)),
    ("Reading log is usually due Friday.", MONDAY_16, (20,)),
    ("If you are absent, the essay is due Monday.", TUESDAY_17, (23,)),
    ("Essay was assigned today and is due Friday.", MONDAY_16, (16, 20)),
    ("Finish the worksheet we were working on by Friday.", MONDAY_16, (20,)),
    ("Anyone who didn't take the quiz must make it up by Friday.", MONDAY_16, (20,)),
    ("Essay due Friday; we will peer review drafts Wednesday.", MONDAY_16, (18, 20)),
    ("Essay was due Friday, now due Monday.", MONDAY_16, (16, 23)),
    ("Cancel" + "led: essay due Friday.", MONDAY_16, (20,)),
    ("Optional for most: essay due Friday.", MONDAY_16, (20,)),
    ("Cancel" + "led! essay due Friday.", MONDAY_16, (20,)),
    ("Optional for most! essay due Friday.", MONDAY_16, (20,)),
    ("Essay - optional for most due Friday", MONDAY_16, (20,)),
    ("Math practice - Reflections and Translations due Monday 11/16", MONDAY_16, (16,)),
    ("Essay could be due Friday.", MONDAY_16, (20,)),
    ("Essay would be due Friday.", MONDAY_16, (20,)),
    ("Essay is supposed to be due Friday.", MONDAY_16, (20,)),
    ("Essay is expected to be due Friday.", MONDAY_16, (20,)),
    ("I think the essay is due Friday.", MONDAY_16, (20,)),
    ("Hopefully the essay is due Friday.", MONDAY_16, (20,)),
    ("Tentative: essay due Friday.", MONDAY_16, (20,)),
    ("TBD: essay due Friday.", MONDAY_16, (20,)),
    ("Possible: essay due Friday.", MONDAY_16, (20,)),
    ("Extra credit: essay due Friday.", MONDAY_16, (20,)),
]


@pytest.mark.parametrize(("words", "day", "days"), UNSET)
def test_a_task_the_reader_does_not_recognize_offers_its_dates_unselected(
    words: str, day: date, days: tuple[int, ...]
) -> None:
    """A task that is not one recognized, complete, affirmative sentence has no deadline and
    offers every date it holds outside a negated or superseded statement, unselected."""
    (task,) = read_one(words, day)

    assert (task.deadline, task.milestones) == (None, ())
    assert offered(task) == tuple(nov(each) for each in days)


SAME_NOTE_CORRECTIONS = [
    "Essay due Friday. Update: now due Monday.",
    "Essay due Friday. It is now due Monday.",
    "Essay due Friday. It's now due Monday.",
    "Essay due Friday. It\N{RIGHT SINGLE QUOTATION MARK}s now due Monday.",
    "Essay due Friday. They're now due Monday.",
    "Essay due Friday. Actually now due Monday.",
    "Essay due Friday. Please note it is now due Monday.",
    "Essay due Friday. Sorry, now due Monday.",
    "Essay due Friday. Update - now due Monday.",
    "Essay due Friday. Update: the essay is now due Monday.",
    "Essay due Friday. Update: it's now due Monday.",
    "Essay due Friday. Correction: it's now due Monday.",
    "Essay due Friday. Please note: it's now due Monday.",
    "Essay due Friday. Quick update: it's now due Monday.",
]


@pytest.mark.parametrize("words", SAME_NOTE_CORRECTIONS)
def test_an_adjacent_correction_naming_no_other_assignment_joins_and_falls_back(
    words: str,
) -> None:
    """A piece whose deadline statement names no separate assignment joins the task before
    it: one task with the teacher's whole wording, both dates offered and no milestone."""
    (task,) = read_one(words, TUESDAY_17)

    assert (task.words, task.deadline, task.milestones) == (words, None, ())
    assert offered(task) == (nov(20), nov(23))


def test_a_correction_naming_another_assignment_stays_its_own_task() -> None:
    """A piece whose lead names an assignment of its own starts its own task, each setting
    its date, each title read from its own lead."""
    essay, project = read_one("Essay due Friday. Project now due Monday.", TUESDAY_17)

    assert (essay.name, essay.deadline, project.name, project.deadline) == (
        "Essay",
        nov(20),
        "Project now",
        nov(23),
    )


@pytest.mark.parametrize(
    ("correction", "name"),
    [
        ("Science project: it's now due Monday.", "Science project"),
        ("The science project, it is now due Monday.", "The science project"),
        ("Math worksheet: this is now due Monday.", "Math worksheet"),
    ],
)
def test_a_correction_under_a_lead_in_naming_another_assignment_is_its_own_task(
    correction: str, name: str
) -> None:
    """A piece whose pronoun follows a lead-in of two or more words sharing none with the
    title of the task before it is a task of its own, titled by the lead-in: the first task
    sets its date, and the second offers its own date unselected."""
    essay, other = read_one(f"Essay due Friday. {correction}", TUESDAY_17)

    assert (essay.name, essay.words, essay.deadline, offered(essay)) == (
        "Essay",
        "Essay due Friday.",
        nov(20),
        (),
    )
    assert (other.name, other.words, other.standing) == (name, correction, HOMEWORK)
    assert (other.deadline, other.milestones, offered(other)) == (None, (), (nov(23),))


def test_a_statement_after_used_to_is_superseded_and_titled_by_its_assignment() -> None:
    """A cue after "used" then "to", with "be" or not, is superseded: the task has no
    deadline, offers no date, keeps its words and is titled by the assignment's name."""
    for words in ("Essay used " + "to be due Friday.", "Essay used " + "to due Friday."):
        (task,) = read_one(words)

        assert (task.name, task.words) == ("Essay", words)
        assert (task.deadline, task.choices, task.named, task.milestones) == (None, (), (), ())


@pytest.mark.parametrize("words", ["Postponed. Essay due Friday.", "Can't wait! Essay due Friday."])
def test_a_sentence_before_the_first_deadline_sentence_joins_its_task(words: str) -> None:
    """A piece with no deadline statement before a note's first deadline sentence joins that
    task: one task named from the deadline sentence, falling back with Friday offered."""
    (task,) = read_one(words)

    assert (task.name, task.words, task.deadline, offered(task)) == (
        "Essay",
        words,
        None,
        (nov(20),),
    )


GOVERNED = [
    ("Essay isn't due Friday.", MONDAY_16, ()),
    ("Essay isn\N{RIGHT SINGLE QUOTATION MARK}t due Friday.", MONDAY_16, ()),
    ("Essay isnt due Friday.", MONDAY_16, ()),
    ("Essay is not due Friday; finish it Monday.", MONDAY_16, (16, 23)),
    ("Nothing is due Friday.", MONDAY_16, ()),
    ("No essay due Friday.", MONDAY_16, ()),
    ("Essay is no " + "longer due Friday.", MONDAY_16, ()),
    ("The essay was due Friday but has been moved to Monday.", TUESDAY_17, (23,)),
    ("Essay due Friday has been moved.", MONDAY_16, ()),
    ("Essay due Friday has been cancel" + "led.", MONDAY_16, ()),
    ("Essay used " + "to be due Friday.", MONDAY_16, ()),
    ("Essay cannot be due Friday.", MONDAY_16, ()),
]


@pytest.mark.parametrize(("words", "day", "days"), GOVERNED)
def test_a_negated_or_superseded_statement_offers_none_of_its_dates(
    words: str, day: date, days: tuple[int, ...]
) -> None:
    """A statement a negation or supersession governs is no deadline and offers no date; a
    date the words name elsewhere is offered as named."""
    (task,) = read_one(words, day)

    assert (task.deadline, task.choices, task.milestones) == (None, (), ())
    assert task.named == tuple(nov(each) for each in days)


MILESTONES = [
    (
        "Draft due Wednesday and final copy due Friday.",
        (Milestone("Draft", nov(18)), Milestone("final copy", nov(20))),
        (),
    ),
    (
        "Draft due Wednesday or Thursday and final copy due Friday.",
        (
            Milestone("Draft", nov(18)),
            Milestone("Draft", nov(19)),
            Milestone("final copy", nov(20)),
        ),
        (),
    ),
    (
        "Essay due Wednesday or Thursday; poster due Friday.",
        (Milestone("Essay", nov(18)), Milestone("Essay", nov(19)), Milestone("poster", nov(20))),
        (),
    ),
    (
        "Draft due Wednesday by 3pm and final copy due Friday.",
        (Milestone("Draft", nov(18)), Milestone("final copy", nov(20))),
        (),
    ),
    ("Essay due Wednesday or by Thursday.", (), (nov(18), nov(19))),
]


@pytest.mark.parametrize(("words", "milestones", "days"), MILESTONES)
def test_clean_labels_keep_every_date_of_their_statement_as_a_milestone(
    words: str, milestones: tuple[Milestone, ...], days: tuple[date, ...]
) -> None:
    """Two or more statements with clean labels keep each of their dates with the label; a
    label that is not clean leaves every date a plain offered date."""
    (task,) = read_one(words)

    assert (task.deadline, task.milestones, offered(task)) == (None, milestones, days)


@pytest.mark.parametrize(
    ("words", "name"),
    [
        ("Essay is not due Friday; finish it Monday", "Essay"),
        ("Lab isn't due Friday.", "Lab"),
        ("Nothing is due Friday.", "Nothing"),
        ("Essay cannot be due Friday.", "Essay"),
        ("Don't forget: essay due Friday.", "Don't forget: essay"),
    ],
)
def test_a_negated_first_statement_names_the_assignment_and_keeps_the_words(
    words: str, name: str
) -> None:
    """A task whose first statement is negated or superseded takes its title from that
    statement's lead; every other title is read as before the rule, and the words stay."""
    (task,) = read_one(words)

    assert (task.name, task.words) == (name, words)


def test_a_separate_no_homework_notice_stays_its_own_task() -> None:
    """A deadline sentence and a separate no-homework notice are two tasks: the first sets
    its date and the notice reads as a notice."""
    essay, notice = read_one("Essay due Friday. No homework due tomorrow.")

    assert (essay.name, essay.deadline, essay.choices, essay.named) == ("Essay", nov(20), (), ())
    assert (notice.name, notice.standing, notice.deadline) == (
        "No homework",
        NOT_ASSIGNMENT,
        nov(17),
    )


def test_two_paragraphs_naming_one_title_without_now_stay_two_tasks() -> None:
    """A later paragraph that names the same title with another date and no "now" is a task of
    its own: each paragraph sets its own date."""
    friday, thursday = read_notes(
        (note("Religion", MONDAY_16, "Short essay due Friday.", "Short essay due Thursday."),)
    )

    assert (friday.name, friday.deadline, thursday.name, thursday.deadline) == (
        "Short essay",
        nov(20),
        "Short essay",
        nov(19),
    )


def test_an_opening_word_task_before_a_dated_sentence_stays_its_own_task() -> None:
    """A head sentence read as homework on its own stays its own task, and the dated sentence
    after it sets its date."""
    study, project = read_notes(
        (
            note(
                "Humanities",
                nov(2),
                "Study for the vocabulary quiz on Thursday. Map project due on Wednesday.",
            ),
        )
    )

    assert (study.name, study.standing, study.deadline) == (
        "Study for the vocabulary quiz on Thursday",
        HOMEWORK,
        None,
    )
    assert (project.name, project.deadline, project.choices) == ("Map project", nov(4), ())


def test_a_head_note_that_names_a_date_stays_its_own_task() -> None:
    """A head paragraph naming a date stays its own task, and the dated paragraph after it
    sets its date."""
    named, lab = read_notes(
        (note("Science", nov(3), "Online practice November 10th", "Lab notebook due on Wednesday"),)
    )

    assert (named.name, named.named, lab.name, lab.deadline) == (
        "Online practice November 10th",
        (nov(10),),
        "Lab notebook",
        nov(4),
    )


def test_a_note_with_no_statement_names_a_two_date_weekday_as_none() -> None:
    """A task holding no deadline statement names only the dates that one expression settles:
    a weekday the school day shares names none."""
    (task,) = read_notes((note("Humanities", nov(5), "Vocabulary quiz on Thursday"),))

    assert (task.deadline, task.choices, task.named) == (None, (), ())


def test_a_no_homework_head_note_stays_its_own_task() -> None:
    """A head sentence that says no homework is assigned stays its own notice beside the dated
    task after it."""
    notice, essay = read_one("No homework tonight. Essay due Friday.")

    assert (notice.standing, essay.name, essay.deadline) == (NOT_ASSIGNMENT, "Essay", nov(20))
