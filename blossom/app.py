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
import pathlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Final

import fastapi
import starlette
from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from blossom.captures import NotACaptureId, capture_id_from
from blossom.clock import Clock
from blossom.dependencies import create_lifespan
from blossom.household import HouseholdGate
from blossom.routes import (
    captures,
    grades,
    hand_in,
    household,
    imports,
    inbox,
    note_details,
    note_links,
    parent,
    school_instructions,
    student,
    verifier,
)
from blossom.routes.forms import FormUnreadable
from blossom.routes.navigation import (
    FAMILY_PAGE,
    WEEK_PAGE,
    address,
    asked_address,
    asked_path,
    note_href,
    segment,
)
from blossom.routes.store_refusals import store_failure
from blossom.routes.student import ReturnLink, parent_reads
from blossom.settings import PageMarks, Settings, get_settings
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.templating import page_templates

logger = logging.getLogger(__name__)
templates = page_templates()

SIGN_IN: Final = "/sign-in"
HELP_ASKS: Final = frozenset(
    {
        "/student/actions/ask-for-help",
        "/student/actions/homework-notes/{capture_id}/ask-for-help",
        "/student/grades/results/{result_id}/ask",
    }
)
"""The routes that send her a request for help rather than save something."""
BACK_TO_HELP: Final = ReturnLink(address(WEEK_PAGE, fragment="help"), "Back to Help")
"""Where a request for help that could not be read goes back to: Help, on her week."""
NOTHING_SAVED: Final = "That form could not be read, so nothing was saved."
NOTHING_SENT: Final = "That form could not be read, so nothing was sent."
SIGN_IN_NOT_READ: Final = "That form could not be read. Open sign-in and try again."
HER_TREE: Final = "/student"
STORE_FAILURES: Final = (sqlite3.Error, StoreBusy, WriterBusy)
"""What the fallback is registered for. Which of them it answers, ``store_failure`` decides."""
SHOWN_AGAIN: Final = frozenset({"GET", "HEAD"})
"""The methods whose fallback page is 503 and offers the same address again."""
PAGE_NOT_SHOWN: Final = "This page can't be shown right now"
RECORDS_UNUSED: Final = "Blossom couldn't use the household's records."
ANSWER_NOT_SHOWN: Final = "This can't be shown right now"
OUTCOME_UNKNOWN: Final = "Blossom couldn't tell whether this went through."
PACKAGE: Final = pathlib.Path(__file__).resolve().parent
ROUTES: Final = PACKAGE / "routes"
FRAMEWORK: Final = (
    pathlib.Path(starlette.__file__).resolve().parent,
    pathlib.Path(fastapi.__file__).resolve().parent,
)
"""The web framework's own folders, where a walk inward from an endpoint stops."""


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


def answers_a_page(route: object) -> bool:
    """Whether the fallback answers a failure of the store on ``route``, the route a request
    matched: one of the application's own routes marked for HTML, by its declaration or by its
    router's. A JSON route, a route that only redirects, and no route at all are left to the
    server."""
    return isinstance(route, APIRoute) and route.response_class is HTMLResponse


def failure_named(error: BaseException) -> str:
    """A failure by its kind alone: its class, and SQLite's name for its code where it has
    one. Never its words, which can quote stored text."""
    name = getattr(error, "sqlite_errorname", None)
    return type(error).__name__ + (f" {name}" if isinstance(name, str) else "")


def where_in_the_routes(error: BaseException, endpoint: Callable[..., object]) -> str:
    """The line in the routes nearest ``error``, as " at routes/<file>:<line> in <function>":
    the deepest frame of its traceback in ``blossom/routes``, among the frames from the
    endpoint's own inward, up to the first frame of the web framework after it. A layer around
    the routes is only ever reached through the framework, so its frame, kept by an error
    raised again, is never named. Empty when there is no such frame."""
    code = getattr(endpoint, "__code__", None)
    step = error.__traceback__
    while step is not None and step.tb_frame.f_code is not code:
        step = step.tb_next
    found = ""
    while step is not None:
        ran = step.tb_frame.f_code
        file = pathlib.Path(ran.co_filename).resolve()
        if any(file.is_relative_to(folder) for folder in FRAMEWORK):
            break
        if file.parent == ROUTES:
            found = f" at {file.relative_to(PACKAGE).as_posix()}:{step.tb_lineno} in {ran.co_name}"
        step = step.tb_next
    return found


def the_address_asked_for(request: Request, route: APIRoute) -> str:
    """The address of this request, as the request sent it where the server gives its path as
    sent. Where it doesn't, which ASGI allows, the matched route and its values make the path,
    since the decoded path has lost which slashes were an id's. An id's escaped slash, question
    mark or number sign stays escaped either way, so the address asks for the same id."""
    raw = request.scope.get("raw_path")
    path = (
        asked_path(raw)
        if isinstance(raw, bytes)
        else route.path_format.format_map(
            {name: segment(str(value)) for name, value in request.path_params.items()}
        )
    )
    return asked_address(path, request.scope["query_string"])


def store_failure_page(
    marks: PageMarks,
) -> Callable[[Request, Exception], Awaitable[HTMLResponse]]:
    """The answer to a failure of the household's store that no page's own recovery caught,
    on a route marked for HTML: a page that reads no store, marked not to be stored, that sets
    no cookie and sends nowhere. A GET or a HEAD is 503, says the page can't be shown, and
    offers the same address again; any other method is 500 and says Blossom couldn't tell
    whether it went through, with a way back and nothing that sends it again. Neither says
    whether anything was saved, or anything about what was typed. The way back follows the
    route's tree and the voice whoever reads. The failure is logged once, by its kind and the
    line in the routes nearest it, never by its words or its traceback.

    Any other error, and any error on a route not marked for HTML, is raised again as itself
    before anything is logged, for the server to answer and report as it does any error. Such
    an error finds the handler twice, at its route and around the router, and is raised again
    both times."""

    async def answer(request: Request, error: Exception) -> HTMLResponse:
        route = request.scope.get("route")
        if not isinstance(route, APIRoute) or not answers_a_page(route) or not store_failure(error):
            raise error
        logger.warning(
            "a store failure reached no page's own recovery: %s %s: %s%s",
            request.method,
            route.path,
            failure_named(error),
            where_in_the_routes(error, route.endpoint),
        )
        again = request.method in SHOWN_AGAIN
        family = route.path == FAMILY_PAGE or route.path.startswith(FAMILY_PAGE + "/")
        hers = route.path == HER_TREE or route.path.startswith(HER_TREE + "/")
        ways_back = []
        if family:
            ways_back = [ReturnLink(FAMILY_PAGE, "Back to Family review")]
        elif hers:
            week = "Back to her week" if parent_reads(request) else "Back to my week"
            ways_back = [ReturnLink(WEEK_PAGE, week)]
        if again:
            ways_back = [link for link in ways_back if link.href != request.scope["path"]]
        return templates.TemplateResponse(
            request,
            "page_unavailable.html",
            {
                "heading": PAGE_NOT_SHOWN if again else ANSWER_NOT_SHOWN,
                "alert": RECORDS_UNUSED if again else OUTCOME_UNKNOWN,
                "again": the_address_asked_for(request, route) if again else None,
                "again_label": "Try again",
                "ways_back": ways_back,
                "family": family,
                "marks": marks,
            },
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if again
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            headers={"Cache-Control": "no-store"},
        )

    return answer


def create_app(
    settings: Settings | None = None,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    real_clock: Clock | None = None,
) -> FastAPI:
    """Build the application without starting it.

    ``settings`` lets a test point the app at a different fixture set without
    touching environment variables; it defaults to the process-wide settings.
    Construction changes nothing outside the returned object. Opening stores
    and forcing hosted tracing off both happen in the lifespan, which
    ``uvicorn`` runs at startup and ``TestClient`` runs only as a context
    manager, so tests use ``with TestClient(app) as client:``. ``monotonic`` is
    the clock runs are timed on, which a test may move by hand, and ``real_clock`` the real
    time a test may pin.
    """
    resolved = get_settings() if settings is None else settings
    app = FastAPI(title="Blossom", lifespan=create_lifespan(resolved, monotonic, real_clock))
    app.mount("/static", StaticFiles(directory=resolved.static_path), name="static")
    app.include_router(household.router)
    app.include_router(student.router)
    app.include_router(hand_in.router)
    app.include_router(captures.router)
    app.include_router(note_details.student_router)
    app.include_router(note_details.family_router)
    app.include_router(note_links.student_router)
    app.include_router(note_links.family_router)
    app.include_router(grades.family_router)
    app.include_router(grades.student_router)
    app.include_router(school_instructions.router)
    app.include_router(parent.router)
    app.include_router(inbox.router)
    # Off, screenshot import has no routes, so each of its addresses, with any method,
    # answers as a missing one.
    if resolved.screenshot_import:
        app.include_router(imports.router)
    app.include_router(verifier.router)
    app.add_exception_handler(FormUnreadable, unreadable_form(resolved.page_marks))
    app.add_exception_handler(grades.NotOnThisComputer, grades.refused_here(resolved.page_marks))
    app.add_exception_handler(imports.ScreenshotsNotHere, imports.refused_here(resolved.page_marks))
    # Registered for these classes, not for every error, it sits inside the gate and the import
    # mark, so its page is marked as any page is; it raises again what it doesn't answer.
    fallback = store_failure_page(resolved.page_marks)
    for failure in STORE_FAILURES:
        app.add_exception_handler(failure, fallback)
    if resolved.screenshot_import:
        app.add_middleware(imports.NoStoreForImports)
    # The gate wraps everything above: with two passphrases set, a page or a
    # route answers only someone who has signed in and may open it.
    app.add_middleware(HouseholdGate, settings=resolved)
    return app


app = create_app()
