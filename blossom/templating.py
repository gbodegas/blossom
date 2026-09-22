"""The one place the pages' templates are built, with the filters both pages use."""

import hashlib
from typing import Final

from fastapi.templating import Jinja2Templates

from blossom.clock import spoken_time
from blossom.plan_reading import long_date
from blossom.plan_text import present_plan
from blossom.routes.navigation import (
    TODAYS_PLAN,
    assignment_anchor,
    details_href,
    hand_in_result_anchor,
    note_action,
    note_add_action,
    note_add_href,
    note_anchor,
    note_details_action,
    note_help_href,
    note_href,
    result_anchor,
    todays_plan_href,
    week_href,
)
from blossom.settings import STATIC_PATH, TEMPLATE_PATH

ASSETS = ("blossom.css", "blossom.js")
"""The packaged files a page links, whose tag changes whenever either does."""


def asset_tag() -> str:
    """A short tag of the stylesheet and the script as packaged, so a page links the
    version it was written for: a device that cached the last release's files fetches
    these afresh rather than running an old script against a new page."""
    digest = hashlib.sha256()
    for name in ASSETS:
        digest.update((STATIC_PATH / name).read_bytes())
    return digest.hexdigest()[:12]


SENTENCE_ENDS: Final = (".", "!", "?", chr(0x2026))
"""What ends a sentence: a period, the two marks, and an ellipsis."""
CLOSING_MARKS: Final = "\"')]" + chr(0x201D) + chr(0x2019)
"""What may follow the end of a sentence and still close it: a quote, straight or curled,
or a bracket."""


def ended(words: str) -> str:
    """``words`` as a sentence that ends once: a period is added only when they do not end
    one themselves, inside a closing quote or bracket included. What someone typed is never
    cut or changed."""
    trimmed = words.rstrip()
    return trimmed if trimmed.rstrip(CLOSING_MARKS).endswith(SENTENCE_ENDS) else f"{trimmed}."


def page_templates() -> Jinja2Templates:
    """The packaged templates, with ``clock`` for times, ``long_date`` for a date with its
    year, ``present`` for a saved plan's text, ``ended`` for typed words that close a
    sentence, ``details_href`` for the address of an
    assignment's details, ``assignment_anchor`` for the id of an assignment's card or row,
    ``week_href`` for her week with one card in view,
    ``result_anchor`` and ``hand_in_result_anchor`` for the places a save's redirect lands
    on, so a page and the address sent to it name a place the same way, ``todays_plan_id``
    and ``todays_plan_href`` for the place on her week that holds today's plan, and
    ``asset_tag`` for the files a page links."""
    templates = Jinja2Templates(directory=TEMPLATE_PATH)
    templates.env.filters["clock"] = spoken_time
    templates.env.filters["ended"] = ended
    templates.env.filters["long_date"] = long_date
    templates.env.filters["present"] = present_plan
    templates.env.globals["asset_tag"] = asset_tag()
    templates.env.globals["assignment_anchor"] = assignment_anchor
    templates.env.globals["details_href"] = details_href
    templates.env.globals["note_href"] = note_href
    templates.env.globals["note_help_href"] = note_help_href
    templates.env.globals["note_action"] = note_action
    templates.env.globals["note_add_href"] = note_add_href
    templates.env.globals["note_add_action"] = note_add_action
    templates.env.globals["note_details_action"] = note_details_action
    templates.env.globals["note_anchor"] = note_anchor
    templates.env.globals["result_anchor"] = result_anchor
    templates.env.globals["hand_in_result_anchor"] = hand_in_result_anchor
    templates.env.globals["week_href"] = week_href
    templates.env.globals["todays_plan_id"] = TODAYS_PLAN
    templates.env.globals["todays_plan_href"] = todays_plan_href
    return templates
