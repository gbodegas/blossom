# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Application assembly: three route trees over one set of stores.

The student, parent and verifier routers are mounted separately rather than
sharing a router with permission checks on individual endpoints. Separate trees
mean a handler cannot accidentally serve the wrong projection, because it has
no access to another principal's view model.

Stores are opened by the lifespan handler, not at import, so importing this
module has no side effects.
"""

import logging
import time
from collections.abc import Callable
from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from blossom.captures import NotACaptureId, capture_id_from
from blossom.dependencies import create_lifespan
from blossom.household import HouseholdGate
from blossom.routes import (
    captures,
    hand_in,
    household,
    inbox,
    note_details,
    note_links,
    parent,
    school_instructions,
    student,
    verifier,
)
from blossom.routes.forms import FormUnreadable
from blossom.routes.navigation import FAMILY_PAGE, WEEK_PAGE, address, note_href
from blossom.routes.student import ReturnLink, parent_reads
from blossom.settings import PageMarks, Settings, get_settings
from blossom.templating import page_templates

logger = logging.getLogger(__name__)
templates = page_templates()

SIGN_IN: Final = "/sign-in"
HELP_ASKS: Final = frozenset(
    {"/student/actions/ask-for-help", "/student/actions/homework-notes/{capture_id}/ask-for-help"}
)
"""The routes that send her a request for help rather than save something."""
BACK_TO_HELP: Final = ReturnLink(address(WEEK_PAGE, fragment="help"), "Back to Help")
"""Where a request for help that could not be read goes back to: Help, on her week."""
NOTHING_SAVED: Final = "That form could not be read, so nothing was saved."
NOTHING_SENT: Final = "That form could not be read, so nothing was sent."
SIGN_IN_NOT_READ: Final = "That form could not be read. Open sign-in and try again."


def unreadable_form(marks: PageMarks) -> Callable[[Request, Exception], HTMLResponse]:
    """The answer to a form the parser could not read, 400, on a page that reads no store.

    What it says and the ways back come from the route alone: sign-in's own words and the way
    back to it, which say nothing about who is signed in and set no cookie; on the family's
    tree, the way back to Family review; on hers, her week named for whoever reads, or Help
    on it for a request for help, and the note when the route names one by an id of a note's
    shape. Nothing of the body is shown, and the failure is logged by its kind alone, since
    the parser's words can quote the body.
    """

    def answer(request: Request, error: Exception) -> HTMLResponse:
        logger.info("a form could not be read: %s", error)
        route = getattr(request.scope.get("route"), "path", request.url.path)
        sign_in = route == SIGN_IN
        family = route.startswith(FAMILY_PAGE + "/")
        if sign_in:
            heading, said = "Sign-in form could not be read", SIGN_IN_NOT_READ
            ways_back = [ReturnLink(SIGN_IN, "Back to sign in")]
        elif family:
            heading, said = "Nothing was saved", NOTHING_SAVED
            ways_back = [ReturnLink(FAMILY_PAGE, "Back to Family review")]
        else:
            asks = route in HELP_ASKS
            heading = "Request not sent" if asks else "Nothing was saved"
            said = NOTHING_SENT if asks else NOTHING_SAVED
            week = "Back to her week" if parent_reads(request) else "Back to my week"
            ways_back = [BACK_TO_HELP if asks else ReturnLink(WEEK_PAGE, week)]
            try:
                note = capture_id_from(request.path_params.get("capture_id", ""))
            except NotACaptureId:
                pass
            else:
                ways_back.insert(0, ReturnLink(note_href(note), "Back to the note"))
        return templates.TemplateResponse(
            request,
            "form_unreadable.html",
            {
                "heading": heading,
                "said": said,
                "ways_back": ways_back,
                "family": family,
                "sign_in": sign_in,
                "marks": marks,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return answer


def create_app(
    settings: Settings | None = None, *, monotonic: Callable[[], float] = time.monotonic
) -> FastAPI:
    """Build the application without starting it.

    ``settings`` lets a test point the app at a different fixture set without
    touching environment variables; it defaults to the process-wide settings.
    Construction changes nothing outside the returned object. Opening stores
    and forcing hosted tracing off both happen in the lifespan, which
    ``uvicorn`` runs at startup and ``TestClient`` runs only as a context
    manager, so tests use ``with TestClient(app) as client:``. ``monotonic`` is
    the clock runs are timed on, which a test may move by hand.
    """
    resolved = get_settings() if settings is None else settings
    app = FastAPI(title="Blossom", lifespan=create_lifespan(resolved, monotonic))
    app.mount("/static", StaticFiles(directory=resolved.static_path), name="static")
    app.include_router(household.router)
    app.include_router(student.router)
    app.include_router(hand_in.router)
    app.include_router(captures.router)
    app.include_router(note_details.student_router)
    app.include_router(note_details.family_router)
    app.include_router(note_links.student_router)
    app.include_router(note_links.family_router)
    app.include_router(school_instructions.router)
    app.include_router(parent.router)
    app.include_router(inbox.router)
    app.include_router(verifier.router)
    app.add_exception_handler(FormUnreadable, unreadable_form(resolved.page_marks))
    # The gate wraps everything above: with two passphrases set, a page or a
    # route answers only someone who has signed in and may open it.
    app.add_middleware(HouseholdGate, settings=resolved)
    return app


app = create_app()
