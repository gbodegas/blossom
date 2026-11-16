# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The entry form's neighboring date groups start level, measured in a browser.

The form to enter one assignment puts the assigned date and the due date side
by side where two columns fit. The due date has help text under it and the
assigned date has none, so the two groups begin level only if the form starts
its rows at the top. These cases render Family review from the code under
test, open the page in a browser, and compare where the two groups begin: on
the page as it arrives, and after a refused due date.

They need a Chromium-based browser, so they are skipped unless
``BLOSSOM_BROWSER`` names its executable, and they do not run in CI::

    BLOSSOM_BROWSER="/path/to/browser" uv run pytest tests/test_entry_form_in_a_browser.py

The browser runs headless, with a new profile, on a page written to the test's
own folder beside a copy of the application's stylesheet and fonts, and prints
the page back with the measured positions in it. Nothing is installed, and the
page asks nothing of a network. A browser makes requests of its own when it
starts with a new profile: it is asked to make none in the background and to
use no proxy, and no host name resolves for it, so a request it still makes by
name connects to nothing. The profile is removed afterward.
"""

import html
import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Final, NamedTuple

import pytest
from fastapi.testclient import TestClient

import blossom
from blossom.app import create_app
from blossom.settings import Settings
from tests.support import SAME_ORIGIN, fixture_settings

BROWSER_VARIABLE: Final = "BLOSSOM_BROWSER"
"""Names the browser's executable. Unset, every case here is skipped."""

WIDTH: Final = 1280
"""A window width at which the form has two columns."""

MEASURE: Final = """<script>
addEventListener("load", () => {
  document.getElementById("add-assignments").open = true;
  document.fonts.ready.then(() => {
    const box = (id) => document.getElementById(id).closest(".field").getBoundingClientRect();
    const assigned = box("entry-assigned_on");
    const due = box("entry-due_date");
    const form = document.querySelector("form.entry");
    const said = document.createElement("pre");
    said.id = "measured";
    said.textContent = JSON.stringify({
      assigned: assigned.top,
      due: due.top,
      beside: due.left - assigned.right,
      taller: due.height - assigned.height,
      columns: getComputedStyle(form).gridTemplateColumns.split(" ").length,
      fonts: [...document.fonts].map((face) => face.status),
    });
    document.body.append(said);
  });
});
</script>"""
"""Opens the Add assignments fold and, once the page's fonts have settled, writes into the
page where the two date groups begin, how far the due date's group is to the right of the
assigned date's and how much taller it is, how many columns the form has, and each font's
state."""

pytestmark = pytest.mark.skipif(
    not os.environ.get(BROWSER_VARIABLE),
    reason=f"{BROWSER_VARIABLE} names no browser to open the page in",
)


class Measured(NamedTuple):
    """What the browser reported about the entry form on one page."""

    assigned: float
    """Where the assigned date's group begins, in pixels from the top of the window."""
    due: float
    """Where the due date's group begins."""
    beside: float
    """From the right edge of the assigned date's group to the left edge of the due date's:
    more than nothing where the two are side by side."""
    taller: float
    """How much taller the due date's group is, for the help text under its control."""
    columns: int
    """How many columns the form's grid has."""
    fonts: frozenset[str]
    """The states of the fonts the stylesheet declares: ``loaded`` alone when every one of
    them has been read."""


def settings_in(tmp_path: pathlib.Path) -> Settings:
    return fixture_settings(
        BLOSSOM_TODAY="2026-09-07",
        BLOSSOM_FIXTURE_PATH="",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )


def measured(page: str, folder: pathlib.Path) -> Measured:
    """What the browser measured on ``page``, written to ``folder`` beside a copy of the
    application's own stylesheet and fonts, each addressed from that folder."""
    static = folder / "static"
    shutil.copytree(pathlib.Path(blossom.__file__).parent / "static", static)
    sheet = static / "blossom.css"
    sheet.write_text(
        sheet.read_text(encoding="utf-8").replace('url("/static/', 'url("'), encoding="utf-8"
    )
    written = folder / "page.html"
    written.write_text(
        page.replace('"/static/', '"static/').replace("</body>", MEASURE + "</body>"),
        encoding="utf-8",
    )
    profile = folder / "profile"
    try:
        shown = subprocess.run(  # noqa: S603  the executable is the one the developer named
            [
                os.environ[BROWSER_VARIABLE],
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--disable-background-networking",
                "--disable-component-update",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--no-proxy-server",
                f"--user-data-dir={profile}",
                f"--window-size={WIDTH},900",
                "--virtual-time-budget=4000",
                "--dump-dom",
                written.as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    found = re.search(r'<pre id="measured">([^<]*)</pre>', shown.stdout)
    assert found is not None, "the browser printed the page without its measures"
    said = json.loads(html.unescape(found.group(1)))
    return Measured(
        assigned=float(said["assigned"]),
        due=float(said["due"]),
        beside=float(said["beside"]),
        taller=float(said["taller"]),
        columns=int(said["columns"]),
        fonts=frozenset(said["fonts"]),
    )


@pytest.mark.parametrize("refused", [False, True], ids=["as it arrives", "a refused due date"])
def test_the_assigned_and_due_date_groups_start_level_where_two_columns_fit(
    refused: bool, tmp_path: pathlib.Path
) -> None:
    """Side by side, the assigned date's group and the due date's group begin at the same
    height, with every font the stylesheet declares loaded, though the due date's group is
    the taller for its help text: on the page as it arrives, and on the page that refuses a
    due date and marks its field."""
    with TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        if refused:
            entry = {"course": "Spanish", "title": "Vocabulary list", "due_date": "next friday"}
            answer = client.post("/parent/inbox/enter", data=entry)
        else:
            answer = client.get("/parent", headers={"Accept": "text/html"})

    assert answer.status_code == (422 if refused else 200)
    assert ('name="due_date" value="next friday" aria-invalid="true"' in answer.text) == refused
    found = measured(answer.text, tmp_path / "shown")
    assert found.columns == 2
    assert found.beside > 0
    assert found.taller > 0
    assert found.fonts == {"loaded"}
    assert found.assigned == pytest.approx(found.due, abs=0.5)
