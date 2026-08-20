"""Keeps the repository describing itself as what it is.

Blossom is built for one household and is expected to outlive any particular
reason it was started. Earlier drafts of the README and the architecture
document framed it as coursework and referred to the design notes by the names
they happen to carry outside this repository. Both were incidental to how the
work began rather than descriptive of what it is, and neither belongs in a
public repository someone might read years from now.

This test does not police tone. It checks for a short list of specific terms
that only ever appeared as a byproduct of that framing, so the wording cannot
drift back without someone noticing.

Note that "checkpoint" on its own is deliberately absent from the banned list.
It is ordinary vocabulary here: a parent sees a periodic checkpoint rather than
a live feed, and LangGraph's state checkpointer is named in the architecture
document. Only the compound forms are excluded.
"""

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

THIS_FILE = pathlib.Path(__file__).name

SEARCHED_FILES = (
    [REPO_ROOT / "README.md"]
    + sorted((REPO_ROOT / "docs").glob("*.md"))
    + sorted((REPO_ROOT / "blossom").rglob("*.py"))
    # This module names the discouraged terms in order to look for them, so it
    # is the one file that must be excluded from its own search.
    + sorted(p for p in (REPO_ROOT / "tests").rglob("*.py") if p.name != THIS_FILE)
)

# Each pattern is paired with what to write instead, so a failure is actionable.
DISCOURAGED = {
    r"\bcapstone\b": "describe the project on its own terms",
    r"\bcourse ?work\b": "describe the project on its own terms",
    r"\bcheckpoint \d": 'refer to "the design notes"',
    r"\bcheckpoint documents?\b": 'refer to "the design notes"',
    r"\bCMU\b": "the institution is not part of what this system is",
}


def test_the_scan_covers_the_documents_that_describe_the_project() -> None:
    """Guard against the check passing because it found nothing to read."""
    names = {path.name for path in SEARCHED_FILES}

    assert "README.md" in names
    assert "architecture.md" in names
    assert len(SEARCHED_FILES) > 20


@pytest.mark.parametrize("pattern,guidance", DISCOURAGED.items())
def test_repository_prose_avoids_incidental_framing(pattern: str, guidance: str) -> None:
    compiled = re.compile(pattern, re.IGNORECASE)
    hits: list[str] = []
    for path in SEARCHED_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if compiled.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{number}")

    assert not hits, f"{pattern} appears at {', '.join(hits)} — {guidance}"


def test_ordinary_uses_of_checkpoint_are_still_allowed() -> None:
    """The bare word is domain vocabulary, and banning it would be wrong.

    A parent's view is a checkpoint by design. This asserts the distinction is
    real rather than accidental, so nobody later "fixes" the parent view's
    wording to satisfy a test that was never aimed at it.
    """
    parent_routes = (REPO_ROOT / "blossom" / "routes" / "parent.py").read_text(encoding="utf-8")

    assert "checkpoint" in parent_routes.lower()
