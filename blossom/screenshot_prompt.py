# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The screenshot reader's prompt and answer schema, exactly as the reader was tested.

The system prompt is prompt v1b: the reading instructions, then the answer schema as JSON
text. The model is asked for JSON by instruction, with no tool or output format, and the
answer is validated in code. Positions are thousandths of the image height.
"""

import json
from typing import Final

SCHEMA_VERSION: Final = "screenshot-reading/1"
"""The answer schema's name, stored with every reading."""

PROMPT_VERSION: Final = "v1b"
"""The prompt's name, stored with every reading."""

CARD_FIELDS: Final = ["badge", "course", "title", "instruction", "footer"]
HEADER_FIELDS: Final = ["school_year", "term", "class_code", "class_name", "teacher"]
CATEGORY_FIELDS: Final = ["name", "weight", "average"]
ROW_FIELDS: Final = [
    "title",
    "due",
    "points",
    "max",
    "average",
    "status",
    "curve",
    "bonus",
    "penalty",
    "note",
]
TERM_FIELDS: Final = ["term_percent", "term_letter"]


def _text(description: str) -> dict[str, object]:
    return {"type": "string", "description": description}


def _maybe(description: str) -> dict[str, object]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}], "description": description}


def _position(description: str) -> dict[str, object]:
    return {
        "type": "integer",
        "description": description
        + " In thousandths of the image height: 0 is the top edge, 1000 the bottom edge.",
    }


def _uncertain(fields: list[str]) -> dict[str, object]:
    return {
        "type": "array",
        "description": (
            "One entry for each field that is covered, cut off, blurred or otherwise not "
            "fully legible. Give a reason instead of a guess."
        ),
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["field", "reason"],
            "properties": {
                "field": {"type": "string", "enum": fields},
                "reason": _text(
                    "Why the field can't be read with certainty, such as 'covered by an "
                    "overlay' or 'cut off at the bottom edge'."
                ),
            },
        },
    }


SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["image_kind", "active_student", "headings", "cards", "report"],
    "properties": {
        "image_kind": {
            "type": "string",
            "enum": ["homework_list", "grade_report", "unsupported"],
            "description": (
                "homework_list: a school portal's homework list with day headings and course "
                "cards. grade_report: a school portal's grade report for one class. "
                "unsupported: anything else, such as a message thread, an email or a photo."
            ),
        },
        "active_student": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "uncertain"],
            "properties": {
                "name": _maybe(
                    "The selected student's name exactly as shown: the highlighted tab of a "
                    "homework list's student picker, or the name on a grade report's student "
                    "line. Names on unselected tabs are never the selected student. null when "
                    "no selected student is visible."
                ),
                "uncertain": _maybe("Why the name can't be read with certainty, or null."),
            },
        },
        "headings": {
            "type": "array",
            "description": (
                "Every day heading of a homework list, top to bottom, such as 'Monday "
                "10/5/2026'. Not the status bar's clock or date, and not the week range."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "y", "uncertain"],
                "properties": {
                    "text": _text("The heading exactly as shown."),
                    "y": _position("The heading's vertical middle."),
                    "uncertain": _maybe("Why the heading can't be read with certainty, or null."),
                },
            },
        },
        "cards": {
            "type": "array",
            "description": (
                "Every course card of a homework list, top to bottom, including cards cut off "
                "by an edge of the image."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "badge",
                    "course",
                    "title",
                    "instruction",
                    "footer",
                    "y_top",
                    "y_bottom",
                    "clipped",
                    "uncertain",
                ],
                "properties": {
                    "badge": {
                        "anyOf": [
                            {"type": "string", "enum": ["DUE", "ASSIGNED"]},
                            {"type": "null"},
                        ],
                        "description": (
                            "The card's DUE or ASSIGNED label, or null if it isn't visible."
                        ),
                    },
                    "course": _maybe(
                        "The course name exactly as shown, or null if it isn't visible."
                    ),
                    "title": _maybe(
                        "The assignment title exactly as shown, line breaks replaced by single "
                        "spaces, or null if it isn't visible."
                    ),
                    "instruction": _maybe(
                        "The teacher's instruction text exactly as shown, line breaks replaced "
                        "by single spaces, or null if the card shows none."
                    ),
                    "footer": _maybe(
                        "The italic due line exactly as shown, such as 'Due (10/8/2026)', or "
                        "null if the card shows none."
                    ),
                    "y_top": _position("The card's top edge."),
                    "y_bottom": _position("The card's bottom edge."),
                    "clipped": {
                        "type": "boolean",
                        "description": (
                            "True when the card runs off the top or bottom edge of the image."
                        ),
                    },
                    "uncertain": _uncertain(CARD_FIELDS),
                },
            },
        },
        "report": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "header_visible",
                        *HEADER_FIELDS,
                        "categories",
                        "term_grade_visible",
                        *TERM_FIELDS,
                        "uncertain",
                    ],
                    "properties": {
                        "header_visible": {
                            "type": "boolean",
                            "description": (
                                "True when the report's header (student, school year, term, "
                                "class, teacher) is in the image."
                            ),
                        },
                        "school_year": _maybe(
                            "As shown, such as '2026-2027'; null if not in the image."
                        ),
                        "term": _maybe("As shown, such as 'Q1'; null if not in the image."),
                        "class_code": _maybe(
                            "The class code as shown, such as 'SCI-08B'; null if not in the image."
                        ),
                        "class_name": _maybe("The class name as shown; null if not in the image."),
                        "teacher": _maybe("The teacher as shown; null if not in the image."),
                        "categories": {
                            "type": "array",
                            "description": "Every grading category in the image, top to bottom.",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "weight", "average", "rows", "uncertain"],
                                "properties": {
                                    "name": _maybe("The category name as shown."),
                                    "weight": _maybe(
                                        "The number after 'Weight =' exactly as shown, such as "
                                        "'40'; null if not in the image."
                                    ),
                                    "average": _maybe(
                                        "The value on the category's 'Category Average' line "
                                        "exactly as shown, such as '82.50%'; \"\" if the line "
                                        "shows no value; null if the line is not in the image."
                                    ),
                                    "rows": {
                                        "type": "array",
                                        "description": (
                                            "The assignment rows, top to bottom. Never the "
                                            "'Category Average' line, the column headers, the "
                                            "term grade or anything outside the table."
                                        ),
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": [
                                                *ROW_FIELDS,
                                                "y_top",
                                                "y_bottom",
                                                "uncertain",
                                            ],
                                            "properties": {
                                                **{
                                                    field: _maybe(
                                                        f"The {field} cell exactly as shown; "
                                                        '"" if the cell is empty; null if it '
                                                        "is not in the image."
                                                    )
                                                    for field in ROW_FIELDS
                                                },
                                                "y_top": _position("The row's top edge."),
                                                "y_bottom": _position("The row's bottom edge."),
                                                "uncertain": _uncertain(ROW_FIELDS),
                                            },
                                        },
                                    },
                                    "uncertain": _uncertain(CATEGORY_FIELDS),
                                },
                            },
                        },
                        "term_grade_visible": {
                            "type": "boolean",
                            "description": "True when the 'Term Grade' line is in the image.",
                        },
                        "term_percent": _maybe(
                            "The term grade's number exactly as shown, such as '79.7'; null if "
                            "not in the image."
                        ),
                        "term_letter": _maybe(
                            "The term grade's letter exactly as shown, such as 'C+'; null if not "
                            "in the image."
                        ),
                        "uncertain": _uncertain([*HEADER_FIELDS, *TERM_FIELDS]),
                    },
                },
            ],
            "description": "For a grade report, its contents; null for any other image.",
        },
    },
}
"""The answer's shape: every object closed, every property required, absence as null."""

PROMPT_V1: Final = (
    "You read one screenshot for a family homework app. Report only what the image shows. "
    "Never infer, complete or correct anything that is cut off, covered or absent, and never "
    "compute or normalize a value.\n"
    "\n"
    'Decide the image kind first. A homework list has day headings such as "Monday '
    "10/5/2026\" and course cards marked DUE or ASSIGNED. A grade report shows one class's "
    'grading categories with "Weight =", assignment rows, "Category Average" lines and a '
    '"Term Grade". Anything else, such as a message thread, is unsupported: return no '
    "headings, no cards and no report for it.\n"
    "\n"
    "The student: give only the selected student, which is the highlighted tab of a homework "
    "list's student picker, or the name on a grade report's student line. Names on unselected "
    "tabs are not the student. If no selected student is visible, the name is null.\n"
    "\n"
    "Homework lists: list every day heading and every card, top to bottom, with their vertical "
    "positions. Copy course, title, instruction and footer verbatim, joining wrapped lines "
    'with single spaces. The footer is the italic due line such as "Due (10/8/2026)". The '
    "status bar's clock or date and the week range are not headings. Mark a card clipped when "
    "it runs off an edge of the image.\n"
    "\n"
    'Grade reports: copy every cell as raw text, exactly as shown. Use "" for a cell that '
    "is visibly empty and null for anything outside the image. Category Average and Term Grade "
    "lines are not assignment rows.\n"
    "\n"
    "When a field is covered, cut off, blurred or otherwise not fully legible, add it to that "
    "item's uncertain list with the reason, and give null or only the part you can read. An "
    "uncertain field is always better than a guess."
)
"""The reading instructions."""

SYSTEM_PROMPT: Final = (
    PROMPT_V1
    + "\n\nAnswer with exactly one JSON object and nothing else: no prose and no code fence. "
    + "It must match this JSON schema, with every property present and null where the schema "
    + "allows it:\n"
    + json.dumps(SCHEMA, separators=(",", ":"))
)
"""Prompt v1b: the instructions, then the schema itself, since the answer is JSON by
instruction alone."""

USER_TEXT: Final = "Read this screenshot."
"""The text sent after the image."""
