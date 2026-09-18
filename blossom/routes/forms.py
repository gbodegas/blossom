"""Reading a form whole, for the pages that take a form and nothing else.

A page's form is a fixed set of fields, each sent once, each text. A form
that arrives otherwise, a field twice, a field of another form's, an
uploaded file among the fields, is not the form the page made, and no
handler writes for it; what it did carry is still handed back, so the page
that refuses can keep what was typed. Her page and the family page read
their forms through the one function here, so the rule is the same at
every door.
"""

from typing import Final

from fastapi import Request

TOKEN_MAX_LENGTH: Final = 200
"""Longer than any id the store makes or a seed carries; a longer one is not looked up."""


async def fields_of(request: Request, allowed: frozenset[str]) -> tuple[dict[str, str], bool]:
    """The form's fields, and whether the form was whole.

    Whole means every field is one this form sends, comes once, and is text:
    a field sent twice, a field of another form's, a channel among them, or
    an uploaded file makes it not whole, and nothing is written for such a
    form. The first text value of each allowed field is still handed back,
    so the page that refuses can keep what was typed.
    """
    form = await request.form()
    fields: dict[str, str] = {}
    whole = True
    for name, value in form.multi_items():
        if name not in allowed or name in fields or not isinstance(value, str):
            whole = False
            continue
        fields[name] = value
    return fields, whole
