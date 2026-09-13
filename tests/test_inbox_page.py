"""The way in, on the family page: paste, look, keep; type, look, keep.

Nothing is written when the text is read; the preview says what keeping it
would do; the keeping reads the text again and writes only what the record
lacks. The text is synthetic, in the portal's shapes.
"""

import pathlib

from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.household import COOKIE
from blossom.routes.inbox import FAR_DATE, NEEDS_BOTH, NOT_A_DATE, NOT_A_KIND, NOTHING_PASTED
from blossom.settings import Settings
from tests.support import fixture_settings

PAGE = {"Accept": "text/html"}
HERS = "quiet mornings and loud music"
THEIRS = "the kitchen table at seven"
SUMMARY = """Homework for Wren

* 09/03/2026 - Thursday
08 Geometry - Assigned: Book Covers: (Due:09/08/2026)
Cover both books with paper.
* 09/04/2026 - Friday
Humanities - Due: Summer Reading - Log:
Spanish - Assigned: Binder, labeled dividers and lined paper check: (Due:09/11/2026)
"""


def settings_in(tmp_path: pathlib.Path, **environ: str) -> Settings:
    """A household with nothing on record, its week pinned to the text's, state in ``tmp_path``."""
    return fixture_settings(
        **{
            "BLOSSOM_TODAY": "2026-09-07",
            "BLOSSOM_FIXTURE_PATH": "",
            "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
            "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
            "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
            **environ,
        }
    )


def test_the_family_page_offers_the_paste_box_and_the_entry_form(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(settings_in(tmp_path))) as client:
        page = client.get("/parent", headers=PAGE).text

    assert "<summary>Put assignments on record</summary>" in page
    assert 'action="/parent/inbox/read"' in page
    assert '<textarea id="paste-text" name="text"' in page
    assert 'action="/parent/inbox/enter"' in page
    assert 'name="course"' in page
    assert '<select name="kind">' in page
    assert '<input type="date" name="due_date">' in page


def test_a_paste_is_shown_first_and_kept_only_when_asked(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
        shown = client.post("/parent/inbox/read", data={"text": SUMMARY})
        nothing_yet = client.get("/student/due-this-week", headers=PAGE).text
        kept = client.post("/parent/inbox/keep", data={"text": SUMMARY})
        family = client.get(kept.headers["location"], headers=PAGE).text
        hers = client.get("/student/due-this-week", headers=PAGE).text
        again = client.post("/parent/inbox/read", data={"text": SUMMARY}).text

    assert shown.status_code == 200
    assert "<h1>What was read</h1>" in shown.text
    assert "These 3 would go on record when put there below, and not before." in shown.text
    assert shown.text.count('<span class="pill">New</span>') == 3
    assert '<span class="pill">Read as a task</span>' in shown.text
    assert "school portal (the assignment&#39;s own line): 2026-09-08" in shown.text
    assert "Assigned Thursday, September 3, 2026" in shown.text
    assert "The teacher wrote: <q>Cover both books with paper.</q>" in shown.text
    assert "Kept with the assignment." in shown.text
    assert "Book Covers" not in nothing_yet
    assert kept.status_code == 303
    assert kept.headers["location"] == "/parent?kept=3&changed=0"
    assert "3 assignments put on record." in family
    assert "Book Covers" in hers
    assert "From the teacher: <q>Cover both books with paper.</q>" in hers
    assert "Reported status:" not in hers.split("Book Covers", 1)[1].split("</article>", 1)[0]
    assert "Binder, labeled dividers and lined paper check" in hers
    assert "Summer Reading - Log" not in hers
    assert again.count("Already on record, nothing new") == 3
    assert "there is nothing to put on it" in again
    assert 'class="primary" disabled aria-disabled="true"' in again


def test_an_entry_by_hand_is_shown_then_kept_as_the_familys_claim(tmp_path: pathlib.Path) -> None:
    entry = {
        "course": "Spanish",
        "title": "Vocabulary list, unit two",
        "due_date": "2026-09-11",
        "assigned_on": "",
        "kind": "HOMEWORK",
    }
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
        shown = client.post("/parent/inbox/enter", data=entry)
        kept = client.post("/parent/inbox/keep", data=entry)
        family = client.get(kept.headers["location"], headers=PAGE).text
        hers = client.get("/student/due-this-week", headers=PAGE).text
        twice = client.post("/parent/inbox/keep", data=entry)

    assert shown.status_code == 200
    assert "This would go on record when put there below, and not before." in shown.text
    assert "family entry: 2026-09-11" in shown.text
    assert '<input type="hidden" name="title" value="Vocabulary list, unit two">' in shown.text
    assert kept.headers["location"] == "/parent?kept=1&changed=0"
    assert "One assignment put on record." in family
    assert "Vocabulary list, unit two" in hers
    assert twice.headers["location"] == "/parent?kept=0&changed=0"


def test_what_cannot_be_read_or_entered_is_said_on_the_family_page(tmp_path: pathlib.Path) -> None:
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
        empty = client.post("/parent/inbox/read", data={"text": "   "})
        stray = client.post("/parent/inbox/read", data={"text": "A stray line nobody expected\n"})
        no_title = client.post("/parent/inbox/enter", data={"course": "Spanish"})
        far = client.post(
            "/parent/inbox/enter",
            data={"course": "Spanish", "title": "Vocabulary list", "due_date": "2030-01-01"},
        )
        bad_date = client.post(
            "/parent/inbox/enter",
            data={"course": "Spanish", "title": "Vocabulary list", "due_date": "next friday"},
        )
        bad_kind = client.post(
            "/parent/inbox/enter",
            data={"course": "Spanish", "title": "Vocabulary list", "kind": "CHORE"},
        )
        nothing = client.get("/parent?kept=0", headers=PAGE).text
        odd = client.get("/parent?kept=%C2%B2", headers=PAGE)
        huge = client.get("/parent?kept=" + "9" * 5000, headers=PAGE)

    assert empty.status_code == 422
    assert NOTHING_PASTED in empty.text
    assert stray.status_code == 200
    assert "Nothing in the text was read as an assignment." in stray.text
    assert "<code>A stray line nobody expected</code>" in stray.text
    assert 'action="/parent/inbox/keep"' not in stray.text
    assert no_title.status_code == 422
    assert NEEDS_BOTH in no_title.text
    assert FAR_DATE in far.text
    assert NOT_A_DATE in bad_date.text
    assert NOT_A_KIND in bad_kind.text
    assert "Nothing new to put on record" in nothing
    assert odd.status_code == 200
    assert huge.status_code == 200
    assert "put on record" not in odd.text.split("<h1>", 1)[1].split("</h1>", 1)[1][:400]


def test_what_the_school_reports_is_shown_on_both_pages_with_its_source_and_day(
    tmp_path: pathlib.Path,
) -> None:
    """The email's "Missing" is kept as a report and shown on her page and the family page
    as a fact the school reported, with where it came from and the day, and the count
    tells a changed row from a new one."""
    email = "Assignments:\n09/09 08 Geometry - A: Homework/Classwork: Book Covers Grade: Missing\n"
    with TestClient(create_app(settings_in(tmp_path)), follow_redirects=False) as client:
        client.post("/parent/inbox/keep", data={"text": SUMMARY})
        shown = client.post("/parent/inbox/read", data={"text": email}).text
        kept = client.post("/parent/inbox/keep", data={"text": email})
        family = client.get(kept.headers["location"], headers=PAGE).text
        hers = client.get("/student/due-this-week", headers=PAGE).text

    assert '<span class="pill">The school reports it missing</span>' in shown
    assert "From the school email, pasted Monday, September 7, 2026." in shown
    assert "On record; a new date claim and what the school reports" in shown
    assert kept.headers["location"] == "/parent?kept=0&changed=1"
    assert "one already on record changed." in family
    assert "<h2>Reported by the school</h2>" in family
    assert "<strong>Book Covers: the school reports it missing.</strong>" in family
    assert "From the school email, pasted Monday, September 7, 2026." in family
    assert "<strong>The school reports this missing.</strong>" in hers
    assert "From the school email, pasted Monday, September 7, 2026." in hers
    assert "Reported status: missing" in hers


def test_the_way_in_is_a_parents(tmp_path: pathlib.Path) -> None:
    settings = settings_in(
        tmp_path,
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        came_in = client.post("/sign-in", data={"passphrase": HERS})
        hers = client.post("/parent/inbox/read", data={"text": SUMMARY}, headers=PAGE)
        her_keep = client.post("/parent/inbox/keep", data={"text": SUMMARY})

    assert COOKIE in came_in.cookies
    assert hers.status_code == 403
    assert her_keep.status_code == 403
