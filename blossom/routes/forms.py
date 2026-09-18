"""Reading a form whole, for the pages that take a form and nothing else.

A page's form is a fixed set of fields, each sent once, each text, and all
of them sent: a browser sends a hidden field and a text field whether or
not anything is in them. A form that arrives otherwise, a field twice, a
field of another form's, an uploaded file among the fields, or a field
left out, is not the form the page made, and no handler writes for it;
what it did carry is still handed back, so the page that refuses can keep
what was typed. The one field a browser leaves out of a form it made is a
group of radio buttons with none chosen, so a caller names such a field
and says for itself that a choice is needed. Her page and the family page
read their forms through the one function here, so the rule is the same
at every door.
"""

from typing import Final

from fastapi import Request

TOKEN_MAX_LENGTH: Final = 200
"""Longer than any id the store makes or a seed carries; a longer one is not looked up."""


async def fields_of(
    request: Request,
    allowed: frozenset[str],
    *,
    may_be_absent: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], bool]:
    """The form's fields, and whether the form was whole.

    Whole means every field is one this form sends, comes once, is text,
    and that every field the form sends came: a field sent twice, a field
    of another form's, a channel among them, an uploaded file, or a field
    left out makes it not whole, and nothing is written for such a form.
    ``may_be_absent`` names the fields a browser leaves out of the page's
    own form, a group of radio buttons with none chosen; the caller asks
    for the choice. The first text value of each allowed field is still
    handed back, so the page that refuses can keep what was typed.
    """
    form = await request.form()
    fields: dict[str, str] = {}
    whole = True
    for name, value in form.multi_items():
        if name not in allowed or name in fields or not isinstance(value, str):
            whole = False
            continue
        fields[name] = value
    whole = whole and fields.keys() >= allowed - may_be_absent
    return fields, whole
