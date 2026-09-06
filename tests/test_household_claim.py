"""One process per household: a second one over the same files is refused before it opens them."""

import pathlib

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.settings import CHECKPOINT_PATH_VARIABLE, DATABASE_PATH_VARIABLE, TRACE_PATH_VARIABLE
from blossom.stores.household_claim import (
    AnotherProcessHasTheHousehold,
    claim_household,
    lock_path_for,
)
from tests.support import PLAN_DATE, fixture_settings


def test_the_lock_file_sits_beside_the_state_file_as_it_really_is(tmp_path: pathlib.Path) -> None:
    straight = tmp_path / "state" / "blossom.sqlite3"
    roundabout = tmp_path / "elsewhere" / ".." / "state" / "blossom.sqlite3"

    assert lock_path_for(straight) == (tmp_path / "state" / "blossom.lock").resolve()
    assert lock_path_for(roundabout) == lock_path_for(straight)


def test_both_state_files_are_claimed_and_a_refused_second_lock_releases_the_first(
    tmp_path: pathlib.Path,
) -> None:
    drafts = tmp_path / "a" / "blossom.sqlite3"
    saved_state = tmp_path / "shared" / "checkpoints.sqlite3"
    other_drafts = tmp_path / "b" / "blossom.sqlite3"
    first = claim_household(drafts, saved_state)
    try:
        with pytest.raises(AnotherProcessHasTheHousehold, match="checkpoints.lock"):
            claim_household(other_drafts, saved_state)
        free_again = claim_household(other_drafts, tmp_path / "b" / "checkpoints.sqlite3")
        free_again.release()
    finally:
        first.release()

    assert first.lock_paths == (lock_path_for(drafts), lock_path_for(saved_state))


def test_a_second_claim_is_refused_until_the_first_is_released(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "state" / "blossom.sqlite3"
    first = claim_household(database)
    try:
        with pytest.raises(AnotherProcessHasTheHousehold, match="blossom.lock") as refused:
            claim_household(database)
    finally:
        first.release()
    second = claim_household(database)
    second.release()

    assert refused.value.lock_path == lock_path_for(database)


def test_a_second_application_over_the_same_files_does_not_start(tmp_path: pathlib.Path) -> None:
    settings = fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        **{
            DATABASE_PATH_VARIABLE: str(tmp_path / "blossom.sqlite3"),
            CHECKPOINT_PATH_VARIABLE: str(tmp_path / "checkpoints.sqlite3"),
            TRACE_PATH_VARIABLE: str(tmp_path / "traces.sqlite3"),
        },
    )
    with TestClient(create_app(settings)) as running:
        assert running.get("/student/due-this-week").status_code == 200
        with pytest.raises(AnotherProcessHasTheHousehold), TestClient(create_app(settings)):
            pass
    with TestClient(create_app(settings)) as after:
        assert after.get("/student/due-this-week").status_code == 200
