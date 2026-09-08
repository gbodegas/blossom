"""The one place the pages' templates are built, with the filters both pages use."""

from fastapi.templating import Jinja2Templates

from blossom.clock import spoken_time
from blossom.plan_text import present_plan
from blossom.settings import TEMPLATE_PATH


def page_templates() -> Jinja2Templates:
    """The packaged templates, with ``clock`` for times and ``present`` for a saved plan."""
    templates = Jinja2Templates(directory=TEMPLATE_PATH)
    templates.env.filters["clock"] = spoken_time
    templates.env.filters["present"] = present_plan
    return templates
