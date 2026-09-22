"""How the record tells that two pieces of homework carry the same class and title.

One rule, kept apart so that everything that asks the question asks it the same
way: the school's paste when it meets the record, and a homework note when it
is about to become homework. The words are compared as written, single-spaced,
with their case and their punctuation. It is a rule about names and nothing
else: it does not look at a date, and two pieces of work that pair may still be
different work, which is for a person to say.
"""


def pair(course: str, title: str) -> tuple[str, str]:
    """The course and title as the record matches them: their words, single-spaced."""
    return " ".join(course.split()), " ".join(title.split())
