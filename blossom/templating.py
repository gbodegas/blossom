"""The one place the pages' templates are built, with the filters both pages use."""

import hashlib

from fastapi.templating import Jinja2Templates

from blossom.clock import spoken_time
from blossom.plan_reading import long_date
from blossom.plan_text import present_plan
from blossom.routes.navigation import details_href
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


def page_templates() -> Jinja2Templates:
    """The packaged templates, with ``clock`` for times, ``long_date`` for a date with its
    year, ``present`` for a saved plan's text, ``details_href`` for the address of an
    assignment's details, and ``asset_tag`` for the files a page links."""
    templates = Jinja2Templates(directory=TEMPLATE_PATH)
    templates.env.filters["clock"] = spoken_time
    templates.env.filters["long_date"] = long_date
    templates.env.filters["present"] = present_plan
    templates.env.globals["asset_tag"] = asset_tag()
    templates.env.globals["details_href"] = details_href
    return templates
