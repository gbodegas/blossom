# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A copy of the family's own state, served for a test pass: every page says "Test copy".

The label stands where "Sample week" stands, in the same markup, from the sign-in page on,
and the two flags are refused together, since one marks synthetic data, the other the
family's own, and a page can't say both. A pinned clock adds the day it is set to, right
after the label or on its own; the real clock adds nothing. A copy starts only from folders
marked as one, on files of its own rather than links, and a marked folder only as a copy.
"""

import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    SAMPLE_VARIABLE,
    TRACE_PATH_VARIABLE,
    Settings,
)
from blossom.stores.paths import (
    TEST_COPY_MARKER,
    CopyMarkError,
    refuse_mismarked_state,
    state_files,
)
from tests.support import (
    HERS,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    fixture_settings,
    linked_to,
    sign_in_as,
)

TEST_COPY = "BLOSSOM_TEST_COPY"
"""The variable a test-copy launch sets, by the name the household's instructions give."""
LABELS = {TEST_COPY: "Test copy", SAMPLE_VARIABLE: "Sample week"}
PINNED = "2026-09-07"
PINNED_LINE = '<p class="note">Today is set to Monday, September 7, 2026.</p>'
SIGNED_IN = {"BLOSSOM_STUDENT_PASSPHRASE": HERS, "BLOSSOM_PARENT_PASSPHRASE": THEIRS}
"""The sign-in on, so a start reads the secret beside the database too."""


def every_kind_of_page(settings: Settings) -> list[str]:
    """Her week, the family review, and the page for a form that could not be read."""
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        pages = [client.get(path).text for path in ("/student/due-this-week", "/parent")]
        unreadable = client.post(
            "/student/actions/homework-notes",
            content=b"note=Seven blue kites",
            headers={"Content-Type": "multipart/form-data"},
        )

    assert unreadable.status_code == 400
    assert "That form could not be read" in unreadable.text
    return [*pages, unreadable.text]


def state_in(folder: Path, *, marked: bool) -> dict[str, str]:
    """The three state paths in ``folder``, marked as a test copy the way the copy step marks
    it when ``marked``."""
    folder.mkdir(parents=True, exist_ok=True)
    if marked:
        (folder / TEST_COPY_MARKER).write_text("", encoding="utf-8")
    return {
        DATABASE_PATH_VARIABLE: str(folder / "blossom.sqlite3"),
        CHECKPOINT_PATH_VARIABLE: str(folder / "checkpoints.sqlite3"),
        TRACE_PATH_VARIABLE: str(folder / "traces.sqlite3"),
    }


def flagged(environment: dict[str, str], folder: Path) -> dict[str, str]:
    """``environment``, served from a marked copy in ``folder`` when it turns the copy on."""
    if environment.get(TEST_COPY, "").strip().lower() in {"1", "on", "true", "yes"}:
        return {**environment, **state_in(folder, marked=True)}
    return environment


def opens_with(page: str, marks: str) -> bool:
    """Whether ``marks`` come first in the page's main content, before anything of its own."""
    return re.search(r'<main id="main"[^>]*>\s*' + re.escape(marks), page) is not None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("1", True),
        ("true", True),
        ("Yes", True),
        ("on", True),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_the_test_copy_flag_reads_yes_and_no(given: str, expected: bool) -> None:
    assert fixture_settings(BLOSSOM_TEST_COPY=given).test_copy is expected


def test_the_test_copy_flag_refuses_anything_else_by_name() -> None:
    with pytest.raises(ValueError, match=TEST_COPY):
        fixture_settings(BLOSSOM_TEST_COPY="maybe")


@pytest.mark.parametrize("sample", ["1", "yes"])
@pytest.mark.parametrize("test_copy", ["1", "on"])
def test_the_sample_and_the_test_copy_are_refused_together_by_name(
    sample: str, test_copy: str
) -> None:
    with pytest.raises(ValueError, match=SAMPLE_VARIABLE) as refused:
        fixture_settings(BLOSSOM_SAMPLE=sample, BLOSSOM_TEST_COPY=test_copy)

    assert TEST_COPY in str(refused.value)


@pytest.mark.parametrize(
    ("environment", "label"),
    [
        ({"BLOSSOM_TEST_COPY": "1"}, "Test copy"),
        ({"BLOSSOM_TEST_COPY": "on", "BLOSSOM_SAMPLE": "no"}, "Test copy"),
        ({"BLOSSOM_SAMPLE": "1"}, "Sample week"),
        ({"BLOSSOM_SAMPLE": "1", "BLOSSOM_TEST_COPY": "0"}, "Sample week"),
    ],
)
def test_every_page_carries_the_one_label_its_flag_sets(
    environment: dict[str, str], label: str, tmp_path: Path
) -> None:
    """On the real clock: the label alone, and no day."""
    for page in every_kind_of_page(fixture_settings(**flagged(environment, tmp_path))):
        assert opens_with(page, f'<p class="sample-label">{label}</p>\n')
        assert page.count('class="sample-label"') == 1
        assert "Today is set to" not in page


def test_with_neither_flag_and_the_real_clock_no_page_carries_a_mark() -> None:
    for page in every_kind_of_page(fixture_settings()):
        assert 'class="sample-label"' not in page
        assert "Sample week" not in page
        assert "Test copy" not in page
        assert "Today is set to" not in page


@pytest.mark.parametrize("flag", [TEST_COPY, SAMPLE_VARIABLE, None])
def test_a_pinned_clock_says_its_day_after_the_label_or_alone(
    flag: str | None, tmp_path: Path
) -> None:
    flags = flagged({flag: "1"} if flag else {}, tmp_path)
    marks = (f'<p class="sample-label">{LABELS[flag]}</p>' if flag else "") + PINNED_LINE

    for page in every_kind_of_page(fixture_settings(BLOSSOM_TODAY=PINNED, **flags)):
        assert opens_with(page, marks)
        assert page.count("Today is set to") == 1


@pytest.mark.parametrize(
    ("flag", "pinned"),
    [(TEST_COPY, PINNED), (SAMPLE_VARIABLE, PINNED), (None, PINNED), (TEST_COPY, None)],
)
def test_the_sign_in_page_and_the_page_for_a_parent_carry_the_same_marks(
    flag: str | None, pinned: str | None, tmp_path: Path
) -> None:
    """With the sign-in on, the first page anyone sees says what the instance is, and so
    does the page that tells her the family review is a parent's."""
    environment = {"BLOSSOM_STUDENT_PASSPHRASE": HERS, "BLOSSOM_PARENT_PASSPHRASE": THEIRS}
    if flag:
        environment[flag] = "1"
    if pinned:
        environment["BLOSSOM_TODAY"] = pinned
    marks = (f'<p class="sample-label">{LABELS[flag]}</p>' if flag else "") + (
        PINNED_LINE if pinned else ""
    )

    app = create_app(fixture_settings(**flagged(environment, tmp_path)))
    with TestClient(app, headers=SAME_ORIGIN, follow_redirects=False) as client:
        sign_in = client.get("/sign-in")
        sign_in_as(client, "her")
        not_hers = client.get("/parent", headers=PAGE_HEADERS)

    assert sign_in.status_code == 200
    assert not_hers.status_code == 403
    for page in (sign_in.text, not_hers.text):
        assert opens_with(page, marks)
        assert page.count("Today is set to") == (1 if pinned else 0)


def test_a_test_copy_refuses_to_start_on_unmarked_folders_and_touches_nothing(
    tmp_path: Path,
) -> None:
    """A path left to its default or to the household's own setting names an unmarked folder:
    the start is refused before anything beside the state files is claimed or opened."""
    folder = tmp_path / "household"
    settings = fixture_settings(**{TEST_COPY: "1"}, **state_in(folder, marked=False))

    with (
        pytest.raises(CopyMarkError, match="holds no TEST-COPY file"),
        TestClient(create_app(settings), headers=SAME_ORIGIN),
    ):
        pass

    assert list(folder.iterdir()) == []


def test_a_marked_folder_refuses_to_start_without_the_flag(tmp_path: Path) -> None:
    folder = tmp_path / "copy"
    settings = fixture_settings(**state_in(folder, marked=True))

    with (
        pytest.raises(CopyMarkError, match="set BLOSSOM_TEST_COPY=1"),
        TestClient(create_app(settings), headers=SAME_ORIGIN),
    ):
        pass

    assert [path.name for path in folder.iterdir()] == [TEST_COPY_MARKER]


def test_every_state_folder_must_carry_the_marker(tmp_path: Path) -> None:
    """One unmarked folder among the three is enough to refuse a copy."""
    marked, unmarked = tmp_path / "copy", tmp_path / "household"
    state_in(marked, marked=True)
    state_in(unmarked, marked=False)
    copy = (marked / "blossom.sqlite3", marked / "checkpoints.sqlite3")
    household = (unmarked / "blossom.sqlite3", unmarked / "checkpoints.sqlite3")

    with pytest.raises(CopyMarkError, match=re.escape(str(unmarked))):
        refuse_mismarked_state(*copy, unmarked / "traces.sqlite3", test_copy=True)
    refuse_mismarked_state(*copy, marked / "traces.sqlite3", test_copy=True)
    with pytest.raises(CopyMarkError, match=re.escape(str(marked))):
        refuse_mismarked_state(*household, marked / "traces.sqlite3", test_copy=False)
    refuse_mismarked_state(*household, unmarked / "traces.sqlite3", test_copy=False)


# ------------------------------------------------- a copy's files are its own, never links


def a_household(folder: Path) -> Path:
    """The family's own state in ``folder``, as a first start with the sign-in on makes it."""
    with TestClient(create_app(fixture_settings(**state_in(folder, marked=False), **SIGNED_IN))):
        pass
    return folder


def a_copy(household: Path, copy: Path) -> dict[str, str]:
    """The copy the household guide describes, flag on: both databases copied into a marked
    folder, and the trace file left for the start to make."""
    environment = state_in(copy, marked=True)
    for name in ("blossom.sqlite3", "checkpoints.sqlite3"):
        shutil.copyfile(household / name, copy / name)
    return {**environment, TEST_COPY: "1", **SIGNED_IN}


def symlinked(target: Path, link: Path) -> None:
    """Put a symbolic link to ``target`` at ``link``, or skip where none can be made."""
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"no symbolic link can be made here: {error}")


def hard_linked(target: Path, link: Path) -> None:
    """Give ``target`` a second name at ``link``, or skip where none can be made."""
    link.unlink(missing_ok=True)
    try:
        link.hardlink_to(target)
    except OSError as error:
        pytest.skip(f"no hard link can be made here: {error}")


def everything_under(root: Path) -> dict[str, bytes | str]:
    """Each file under ``root`` with its bytes, and each link with its target, unfollowed."""
    found: dict[str, bytes | str] = {}
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_symlink() or entry.is_junction():
                found[entry.path] = os.readlink(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                found[entry.path] = "a folder"
                found.update(everything_under(Path(entry.path)))
            else:
                found[entry.path] = Path(entry.path).read_bytes()
    return found


def refused(environment: dict[str, str], root: Path, match: str) -> None:
    """A start on ``environment`` is refused, and nothing under ``root`` is made or changed."""
    before = everything_under(root)

    with (
        pytest.raises(CopyMarkError, match=match),
        TestClient(create_app(fixture_settings(**environment))),
    ):
        pass

    assert everything_under(root) == before


def test_a_copy_made_as_the_guide_says_starts_and_leaves_the_household_alone(
    tmp_path: Path,
) -> None:
    household = a_household(tmp_path / "household")
    copy = tmp_path / "copy"
    environment = a_copy(household, copy)
    before = everything_under(household)
    assert not (copy / "traces.sqlite3").exists()

    app = create_app(fixture_settings(**environment))
    with TestClient(app, headers=SAME_ORIGIN, follow_redirects=False) as client:
        sign_in_as(client, "her")
        page = client.get("/student/due-this-week", headers=PAGE_HEADERS)

    assert page.status_code == 200
    assert '<p class="sample-label">Test copy</p>' in page.text
    assert everything_under(household) == before
    made = {"traces.sqlite3", "blossom.lock", "checkpoints.lock", "household.secret"}
    assert made <= {path.name for path in copy.iterdir()}


def test_the_files_checked_are_every_file_a_running_copy_makes(tmp_path: Path) -> None:
    """Each database with its SQLite files, both locks and the secret, and nothing else."""
    copy = tmp_path / "copy"
    environment = a_copy(a_household(tmp_path / "household"), copy)
    databases = ("blossom.sqlite3", "checkpoints.sqlite3", "traces.sqlite3")
    checked = state_files(*(copy / name for name in databases))

    with TestClient(create_app(fixture_settings(**environment))):
        running = {path.name for path in copy.iterdir()}

    assert [path.parent for path in checked] == [copy] * len(checked)
    assert sorted(path.name for path in checked) == sorted(
        [
            *(name + suffix for name in databases for suffix in ("", "-journal", "-wal", "-shm")),
            "blossom.lock",
            "checkpoints.lock",
            "household.secret",
        ]
    )
    assert running - {TEST_COPY_MARKER} <= {path.name for path in checked}


@pytest.mark.parametrize(
    ("link", "name"),
    [
        pytest.param(hard_linked, "blossom.sqlite3", id="hard-linked database"),
        pytest.param(symlinked, "blossom.sqlite3", id="symlinked database"),
        pytest.param(symlinked, "checkpoints.sqlite3", id="symlinked saved state"),
        pytest.param(hard_linked, "checkpoints.sqlite3-wal", id="hard-linked write-ahead log"),
        pytest.param(symlinked, "checkpoints.sqlite3-wal", id="symlinked write-ahead log"),
        pytest.param(hard_linked, "checkpoints.sqlite3-shm", id="hard-linked log index"),
        pytest.param(symlinked, "blossom.sqlite3-journal", id="symlinked journal"),
        pytest.param(hard_linked, "traces.sqlite3-journal", id="hard-linked trace journal"),
        pytest.param(hard_linked, "blossom.lock", id="hard-linked lock"),
        pytest.param(symlinked, "checkpoints.lock", id="symlinked lock"),
        pytest.param(hard_linked, "household.secret", id="hard-linked secret"),
        pytest.param(symlinked, "household.secret", id="symlinked secret"),
    ],
)
def test_a_copy_holding_a_link_to_a_household_file_is_refused(
    link: Callable[[Path, Path], None], name: str, tmp_path: Path
) -> None:
    """Whatever the file, a write through the link would land in the household's."""
    household = a_household(tmp_path / "household")
    copy = tmp_path / "copy"
    environment = a_copy(household, copy)
    if not (household / name).exists():
        (household / name).write_bytes(b"synthetic, as a running household holds it")
    link(household / name, copy / name)

    refused(environment, tmp_path, match=re.escape(f"{copy / name} "))


def test_a_dangling_link_into_the_household_is_refused(tmp_path: Path) -> None:
    """A start would make the missing file at the far end, in the household's folder."""
    household = a_household(tmp_path / "household")
    copy = tmp_path / "copy"
    environment = a_copy(household, copy)
    (household / "traces.sqlite3").unlink()
    symlinked(household / "traces.sqlite3", copy / "traces.sqlite3")

    refused(environment, tmp_path, match=re.escape(f"{copy / 'traces.sqlite3'} "))


def a_dangling_link(copy: Path, name: str) -> None:
    symlinked(copy / "moved.sqlite3", copy / name)


def a_loop(copy: Path, name: str) -> None:
    symlinked(copy / "loop", copy / name)
    symlinked(copy / name, copy / "loop")


def a_folder(copy: Path, name: str) -> None:
    (copy / name).mkdir()


def a_junction(copy: Path, name: str) -> None:
    if linked_to(copy.parent / "household", copy / name) is None:
        pytest.skip("no junction or symlink can be made here")


@pytest.mark.parametrize(
    ("make", "name"),
    [
        pytest.param(a_dangling_link, "traces.sqlite3", id="dangling link"),
        pytest.param(a_loop, "traces.sqlite3", id="link loop"),
        pytest.param(a_folder, "traces.sqlite3", id="folder"),
        pytest.param(a_junction, "checkpoints.sqlite3-shm", id="junction"),
    ],
)
def test_a_copy_whose_state_file_is_not_a_plain_file_is_refused(
    make: Callable[[Path, str], None], name: str, tmp_path: Path
) -> None:
    copy = tmp_path / "copy"
    environment = a_copy(a_household(tmp_path / "household"), copy)
    make(copy, name)

    refused(environment, tmp_path, match=re.escape(f"{copy / name} "))


def test_a_copy_whose_folder_leads_into_the_household_is_refused(tmp_path: Path) -> None:
    household = a_household(tmp_path / "household")
    copy = linked_to(household, tmp_path / "copy")
    if copy is None:
        pytest.skip("no junction or symlink can be made here")

    refused(
        {**state_in(copy, marked=False), TEST_COPY: "1", **SIGNED_IN},
        tmp_path,
        match="holds no TEST-COPY file",
    )


def linked_database(household: Path, copy: Path) -> Path:
    symlinked(copy / "blossom.sqlite3", household / "blossom.sqlite3")
    return household


def linked_log(household: Path, copy: Path) -> Path:
    (copy / "checkpoints.sqlite3-wal").write_bytes(b"synthetic write-ahead log")
    symlinked(copy / "checkpoints.sqlite3-wal", household / "checkpoints.sqlite3-wal")
    return household


def linked_folder(household: Path, copy: Path) -> Path:
    folder = linked_to(copy, household.parent / "plainly-named")
    if folder is None:
        pytest.skip("no junction or symlink can be made here")
    return folder


def marked_in_lower_case(household: Path, copy: Path) -> Path:
    (household / TEST_COPY_MARKER.lower()).write_text("", encoding="utf-8")
    return household


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param(linked_database, id="database linked into a marked folder"),
        pytest.param(linked_log, id="write-ahead log linked into a marked folder"),
        pytest.param(linked_folder, id="folder linked to a marked one"),
        pytest.param(marked_in_lower_case, id="marker in lower case"),
    ],
)
def test_without_the_flag_a_marked_folder_however_reached_is_refused(
    lead: Callable[[Path, Path], Path], tmp_path: Path
) -> None:
    """A copy served without its label would read as the household's own."""
    household = a_household(tmp_path / "household")
    copy = tmp_path / "copy"
    a_copy(household, copy)
    folder = lead(household, copy)

    refused(
        {**state_in(folder, marked=False), **SIGNED_IN},
        tmp_path,
        match="set BLOSSOM_TEST_COPY=1",
    )
