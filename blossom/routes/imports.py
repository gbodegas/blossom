# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Homework screenshots added in Family review: the upload, the images, and a Read press.

Every route serves a parent alone: with the sign-in on the household gate turns away her and
anyone not signed in, and with it off only this computer is served. No route takes a form
parameter, so that check runs before any byte of a body is read. Every answer is marked
no-store, sign-in on or off. Addresses carry a review's id and an image's id, nothing else.

An upload's body is read here, through ``uploads.read_upload``, within every limit; its images
are prepared before the draft store's one write. A confirmed successful upload answers with a
redirect to its review, so a reload sends nothing again; what that upload refused or found
already there is said once on the review, from a short signed note kept in a cookie for that
review alone. A write that commits and then raises can answer the page that says its outcome
isn't known.
"""

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Final, NamedTuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from blossom.anthropic_client import model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.household import UnreadableHouseholdSecret
from blossom.routes.forms import FormUnreadable, WithinBytes, fields_of, form_of
from blossom.routes.grades import from_this_computer, grade_name_key
from blossom.routes.navigation import week_href
from blossom.routes.store_refusals import busy_file as busy_file
from blossom.screenshot_page import (
    CORRECT_PRESS,
    NEEDED,
    READY_ASKING,
    READY_WHOLE,
    SELECTION_CHECK,
    SELECTION_SHOWN,
    Box,
    ImageName,
    ListTooLarge,
    PostRefused,
    RowsShown,
    answer_field,
    cards_named,
    change_said,
    kept_ref,
    labels_for,
    page_check,
    piece_text,
    rows_shown,
    same_signature,
    selected_on_load,
    selection_read,
    signed_action,
)
from blossom.screenshot_page import SAID as PAGE_SAID
from blossom.screenshot_posts import (
    PROGRESS_SHAPE,
    ReviewPost,
    answer_pieces,
    kept_assignments,
    progress_read,
    progress_text,
    read_change,
    read_review,
    rows_of_units,
    selection_text,
    shape_four,
    typed_as_sent,
    typed_beside_words,
    typed_for,
    words_box,
)
from blossom.screenshot_reading import ReadOutcome, read_screenshots
from blossom.screenshot_review import (
    UNJOINED,
    Corrections,
    change_told,
    dates_key,
    dest_key,
    dest_kind,
    offered_as_it_stands,
    unit_name,
    value_json,
    value_of,
)
from blossom.screenshot_save import (
    AlreadySaved,
    NotSaved,
    Replayed,
    ReviewState,
    Saved,
    SaveOutcome,
    SavePress,
    canonical,
    confirm_subject,
    correction_text,
    fold_reads,
    read_digest,
    review_state,
    save_homework,
    student_line,
    unit_digests,
    where_it_failed,
)
from blossom.screenshots import homework_assembly
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.homework_saves import (
    LEFT_OPEN_ON_THE_RECORD,
    DraftForSave,
    TransactionLeftOpen,
)
from blossom.stores.import_drafts import (
    DRAFT_LIFETIME,
    MOST_IMAGES_PRESENT,
    TOKEN_SHAPE,
    Added,
    AnswersKept,
    AnswerToKeep,
    AttemptUnderWay,
    Closing,
    DraftClosed,
    DraftExpired,
    DraftFrozen,
    DraftNotFound,
    DraftSnapshot,
    ReviewChanged,
    TooManyImages,
    UnknownImage,
    new_creation_token,
)
from blossom.stores.paths import UnsafeCheckpointPath
from blossom.templating import answering, page_templates
from blossom.uploads import Upload, UploadRefused, prepare_upload, read_upload

PREFIX: Final = "/parent/imports"
HOMEWORK: Final = "homework_screenshot"
FILE_FIELD: Final = "screenshots"
FIRST_FIELDS: Final = {"token": 32, "issued": 40}
"""The text fields a first upload's form sends, each with its most bytes: the creation token
and the instant its page was made, read once each."""
NOTE_COOKIE: Final = "blossom_upload_note"
NOTE_SECONDS: Final = 300
"""How long an upload's note waits for its review to show it."""

LABELS: Final = {
    "unread": "Not read yet.",
    "ended": "Not read. Read again.",
    "read": "Read.",
    "wrong_kind": "This doesn't look like the school's homework list.",
}
"""How an image stands, as its review says it, all in one place: an image no attempt covers,
one whose attempt ended with no result kept, one read, and one read as something else. "Read
again" is never said of an image never read."""

FAILED: Final = {
    "not_read_in_time": "Not read in time. Read again.",
    "service_busy": "The reading service was busy. Read again.",
    "service_refused": "The reading service refused the request. Read again.",
    "model_refused": "The reader declined this screenshot. Read again or remove it.",
    "cut_off": "The reading was cut off. Read again.",
    "not_json": "What was read couldn't be used. Read again.",
    "not_valid": "What was read couldn't be used. Read again.",
}
"""Each way a read can fail, as the image's label says it."""

UPLOAD_SAID: Final = {
    "body_too_large": "Nothing was added: the screenshots came to more than 40 MB together.",
    "too_many_parts": "Nothing was added: more than 10 files were sent at once.",
    "too_many_files": "Nothing was added: more than 10 files were sent at once.",
    "file_too_large": "Nothing was added: one file is over 12 MB.",
    "not_multipart": "Nothing was added: the upload couldn't be read.",
    "unknown_field": "Nothing was added: the upload couldn't be read.",
    "repeated_field": "Nothing was added: the upload couldn't be read.",
    "field_too_large": "Nothing was added: the upload couldn't be read.",
    "unreadable": "Nothing was added: the upload couldn't be read.",
}
"""Why an upload was refused whole, by count and size only."""
SIZE_REFUSALS: Final = frozenset(
    {"body_too_large", "too_many_parts", "too_many_files", "file_too_large"}
)

IMAGE_SAID: Final = {
    "not_png_or_jpeg": "Screenshot {n} wasn't added: it isn't a PNG or JPEG.",
    "heic": "Screenshot {n} wasn't added: it's a HEIC photo. Save it as JPEG or PNG first.",
    "too_many_pixels": "Screenshot {n} wasn't added: it has more than 30 million pixels.",
    "several_frames": "Screenshot {n} wasn't added: it's an animated image.",
    "undecodable": "Screenshot {n} wasn't added: it couldn't be opened.",
    "duplicate": "Screenshot {n} was already in this review.",
}
"""What became of one image of an upload, by its place in that upload, counted from one."""

SAID: Final = {
    "absent": "This review isn't available.",
    "expired": (
        "This review expired. Anything you already saved is safe. Add the screenshots again "
        "to continue."
    ),
    "closed": "This review is finished; there's nothing more to review.",
    "too_old": "This form is too old to use. Choose the screenshots again.",
    "busy": "Blossom was busy, so nothing was added. Send the screenshots again.",
    "image_gone": "That screenshot isn't in this review.",
    "frozen": (
        "This review's screenshots are fixed now that some homework is saved. To add or change "
        "a screenshot, start a new upload; Blossom compares it with what is saved."
    ),
    "unchecked": (
        "Nothing was read: this page's Read button couldn't be checked. Press Read again."
    ),
    "no_key": "Screenshots can't be read here: Blossom has no API key.",
    "only_here": (
        "Screenshots can be added only on the computer running Blossom, or with sign-in on."
    ),
    "read_busy": "Blossom was busy, so nothing was read. Press Read again.",
    "no_name_check": (
        "Blossom couldn't check that these screenshots are hers. See the household guide before "
        "saving."
    ),
}
LIMITS: Final = (
    "Up to 10 screenshots at a time, PNG or JPEG, up to 12 MB each. A review holds up to 10 "
    "screenshots."
)
READING: Final = "Reading the screenshots"
"""What the Read press says while it waits; the script's pulsing dots after it are its
ellipsis."""
DISCLOSED: Final = (
    "These screenshots will be sent to Anthropic's API to be read. Nothing is sent until you "
    "press Read."
)

templates = page_templates()
logger = logging.getLogger(__name__)


def image_refusal(place: int, kind: str) -> str:
    """One image's refusal or duplicate, by its place in the upload, counted from one."""
    return IMAGE_SAID[kind].format(n=place)


def too_many(free: int) -> str:
    """Why an upload that would pass ten images committed nothing."""
    room = (
        "no more screenshots"
        if free == 0
        else f"{free} more screenshot" + ("" if free == 1 else "s")
    )
    return f"Nothing was added. This review has room for {room}; remove one to make room."


def busy_reading(place: int) -> str:
    """A reading the busy file couldn't keep, by the image's place in its review."""
    return f"Blossom was busy and couldn't keep the reading of screenshot {place}. Read again."


def under_way(until: str) -> str:
    """What a press says while an attempt is current, with the time it ends."""
    return f"Reading is under way until {until}."


class ScreenshotsNotHere(Exception):
    """An import route asked with sign-in off from anywhere but the computer running Blossom;
    nothing was read and nothing was written."""


def screenshots_here(request: Request) -> None:
    """The import routes' dependency: with sign-in off, only this computer, decided before any
    body byte is read. With import off the routes aren't registered at all, so every import
    address answers as a missing one, whatever its method."""
    state = get_application_state(request)
    if not state.settings.household_sign_in and not from_this_computer(request):
        raise ScreenshotsNotHere


router = APIRouter(
    prefix=PREFIX,
    dependencies=[Depends(screenshots_here)],
    include_in_schema=False,
    default_response_class=HTMLResponse,
)
State = Annotated[ApplicationState, Depends(get_application_state)]


class NoStoreForImports:
    """Mark every answer the application makes to an import address no-store, whatever made
    it: a route, a refusal handler or a missing address. The server's bare 500 for an error no
    page catches is made outside this layer and carries no mark."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(PREFIX):
            await self.app(scope, receive, send)
            return

        async def marked(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, marked)


def refused_here(marks: object) -> Callable[[Request, Exception], Awaitable[Response]]:
    """The answer to ``ScreenshotsNotHere``: a page saying where screenshots can be added."""

    async def answer(request: Request, error: Exception) -> Response:
        return said_page(request, marks, SAID["only_here"], 403)

    return answer


def said_page(request: Request, marks: object, said: str, status: int, *more: str) -> Response:
    """A page that says why nothing more happened, its first sentence as the alert, which takes
    the focus when the page answers a press and not when an address opened it."""
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "said": [said, *more],
            "marks": marks,
            "page": "parent",
        },
        status_code=status,
    )


def marks_of(state: ApplicationState) -> object:
    """The household's page marks, which every page shows."""
    return state.settings.page_marks


def role_of(state: ApplicationState) -> str:
    """Who makes a review: a signed-in parent, or this computer with the sign-in off."""
    return "parent" if state.settings.household_sign_in else "household"


def opaque(value: str) -> bool:
    """Whether an id from an address has the shape the store gives ids: 32 hex characters."""
    return TOKEN_SHAPE.fullmatch(value) is not None


# ------------------------------------------------------------------ the note an upload leaves


def note_check(key: bytes, body: str) -> str:
    """The signature of an upload's note under the application's key."""
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()[:32]


class Note(NamedTuple):
    """What a press says on the review it redirects to: its problems, its status lines, and a
    save's outcome lines, each shown in its own style."""

    said: list[str]
    told: list[str]
    done: list[str]


def note_for(
    key: bytes,
    draft_id: str,
    said: Sequence[str],
    told: Sequence[str] = (),
    done: Sequence[str] = (),
) -> str:
    """A note for one review: what a press found wrong, what it did and what a save did,
    signed."""
    payload = json.dumps([draft_id, list(said), list(told), list(done)])
    body = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{body}.{note_check(key, body)}"


def note_read(key: bytes, draft_id: str, value: str | None) -> Note:
    """The lines a note holds for this review, or none when it is another's or unsigned. A
    note of two parts holds problems only."""
    nothing = Note([], [], [])
    if not value or "." not in value:
        return nothing
    body, check = value.rsplit(".", 1)
    if not same_signature(check, note_check(key, body)):
        return nothing
    try:
        found = json.loads(base64.urlsafe_b64decode(body.encode()))
    except (ValueError, TypeError):
        return nothing
    if not isinstance(found, list) or len(found) not in (2, 4) or found[0] != draft_id:
        return nothing
    parts = [part if isinstance(part, list) else [] for part in found[1:]]
    lines = [[str(line) for line in part if isinstance(line, str)] for part in parts]
    return Note(*lines) if len(lines) == 3 else Note(lines[0], [], [])


def to_review(
    state: ApplicationState,
    draft_id: str,
    said: Sequence[str] = (),
    told: Sequence[str] = (),
    done: Sequence[str] = (),
) -> Response:
    """The redirect every committed press answers with, carrying its note when it has one."""
    answer = RedirectResponse(f"{PREFIX}/{draft_id}", status_code=303)
    if said or told or done:
        answer.set_cookie(
            NOTE_COOKIE,
            note_for(state.result_key, draft_id, said, told, done),
            max_age=NOTE_SECONDS,
            path=f"{PREFIX}/{draft_id}",
            httponly=True,
            samesite="strict",
        )
    return answer


def uploaded_said(added: int, total: int, read: int) -> list[str]:
    """What an upload says it added, from what the review holds after it: nothing when it added
    none; the first upload's count; or the count it added beside the review's."""
    if added == 0:
        return []
    noun = "screenshot" if added == 1 else "screenshots"
    if added == total:
        return [f"{added} {noun} uploaded. None read yet."]
    return [f"{added} more {noun} uploaded. {total} in this review, {read} read."]


def upload_said(
    added: Added, places: Sequence[int], refused: Sequence[tuple[int, str]]
) -> list[str]:
    """What an upload's note says, by place in the upload: each image refused, and each one
    already in the review."""
    said = [(place, kind) for place, kind in refused]
    said += [(places[index], "duplicate") for index in added.duplicates]
    return [image_refusal(place + 1, kind) for place, kind in sorted(said)]


# ------------------------------------------------------------------ uploads


async def body_of(request: Request, fields: Mapping[str, int]) -> Upload:
    """An upload's body, read from its stream within every limit."""
    return await read_upload(
        request.headers.get("content-type"),
        request.headers.get("content-length"),
        request.stream(),
        fields=fields,
        file_field=FILE_FIELD,
    )


def refused_upload(request: Request, state: ApplicationState, refusal: UploadRefused) -> Response:
    """An upload refused whole: 413 for a limit of count or size, 400 for a body out of shape."""
    status = 413 if refusal.kind in SIZE_REFUSALS else 400
    return said_page(request, marks_of(state), UPLOAD_SAID[refusal.kind], status)


def issued_in_time(state: ApplicationState, issued: str | None) -> bool | None:
    """Whether a first upload's page was made less than 48 hours ago and not after now; None
    when its instant can't be read."""
    if issued is None:
        return None
    try:
        made = datetime.fromisoformat(issued)
    except ValueError:
        return None
    if made.tzinfo is None:
        return None
    now = state.real_clock.now()
    return made <= now and now - made < DRAFT_LIFETIME


def busy_refusal(error: Exception) -> bool:
    """Whether a refusal is a busy store's: the drafts store's own, or the file's by its code."""
    if isinstance(error, StoreBusy | WriterBusy):
        return True
    return isinstance(error, sqlite3.Error) and busy_file(error)


def noted(error: Exception) -> None:
    """A failure of the household's file that isn't a busy refusal, in the log by its kind
    alone, never its words, so a failure a page answers is still on record. A busy refusal isn't
    logged, as the busy pages never were. A transaction left open is its own diagnosis, at
    error level, and the store that refused it has logged it where it raised, once, so nothing
    is added here."""
    if isinstance(error, TransactionLeftOpen):
        return
    if isinstance(error, sqlite3.Error) and not busy_file(error):
        logger.warning("an import page's store call failed: %s", type(error).__name__)


def acceptance_unread(error: sqlite3.Error) -> None:
    """Why a Save press couldn't ask whether it has saved. The page is the same for a store that
    refused the read and for a transaction left open, and the log tells them apart, by kind: the
    store has logged a transaction left open where it raised."""
    if isinstance(error, TransactionLeftOpen):
        return
    what = "busy" if busy_file(error) else type(error).__name__
    logger.warning("A Save press's acceptance couldn't be read: %s", what)


def noted_after_commit(error: Exception) -> None:
    """A failure while making the page of a press known to have committed. A store's is noted as
    any is. Anything else is an unexpected error, kept from the server only here, once the press
    has committed, since a plain error would hide a save that happened: it goes on record at
    error level by its type and place alone, never its words or a traceback's text, and never as
    a busy store."""
    if isinstance(error, STORE_FAILURES):
        noted(error)
        return
    logger.error(
        "A page after a committed save failed unexpectedly: %s%s",
        type(error).__name__,
        where_it_failed(error),
    )


def upload_refusal(request: Request, state: ApplicationState, error: Exception) -> Response:
    """A store's refusal of an upload: a busy store wrote nothing, so the screenshots weren't
    added and sending them again is what works; any other refusal as a press's."""
    if busy_refusal(error):
        return said_page(request, marks_of(state), SAID["busy"], 503)
    return store_refusal(request, state, error)


def store_refusal(
    request: Request, state: ApplicationState, error: Exception, answers: Sequence[str] = ()
) -> Response:
    """A store's refusal of a request on a review or one of its images, or of an upload for
    any reason but a busy store, as its page says it. A busy store began nothing, so the
    request changed nothing. The refusal's own sentence is the page's first, which the page
    draws as its alert and gives the focus on a press. A press whose own write stored or dropped
    answers before a read was refused says its answers lines (``answers``) after that sentence."""
    if isinstance(error, TooManyImages):
        said, status = too_many(error.free), 409
    elif isinstance(error, DraftClosed):
        said, status = SAID["closed"], 409
    elif isinstance(error, DraftExpired):
        said, status = SAID["expired"], 410
    elif isinstance(error, DraftFrozen):
        said, status = SAID["frozen"], 409
    elif busy_refusal(error):
        said, status = AFTER_SAID["busy"], 503
    else:
        said, status = SAID["absent"], 404
    return said_page(request, marks_of(state), said, status, *answers)


def unread_page(
    request: Request, state: ApplicationState, error: Exception, *, upload: bool = False
) -> Response:
    """The household's file refusing a read before anything was written: a busy file as the
    route says a busy store, an upload's or a press's; any other refusal, for an upload, that
    nothing was added, since its write never began and a review may not exist yet, and for a
    press or a page asked for, as the review that can't be shown, which says nothing of what
    was saved."""
    if not busy_refusal(error):
        said = NOTHING_ADDED if upload else REVIEW_UNAVAILABLE
        return said_page(request, marks_of(state), said, 503)
    return upload_refusal(request, state, error) if upload else store_refusal(request, state, error)


def unwritten_page(
    request: Request,
    state: ApplicationState,
    error: Exception,
    unknown: str,
    *,
    upload: bool = False,
) -> Response:
    """The household's file refusing a write: a busy file wrote nothing, as the route says a
    busy store; for any other refusal whether the write committed can't be known, as
    ``unknown``, the press's own sentence, says."""
    if not busy_refusal(error):
        return said_page(request, marks_of(state), unknown, 503)
    return upload_refusal(request, state, error) if upload else store_refusal(request, state, error)


TRY_UPLOAD_AGAIN: Final = "To try this upload again, choose the same screenshots below."
"""What a first upload's page says under its sentence when its outcome isn't known, above the
form that sends screenshots with that upload's own token."""


def unwritten_upload(
    request: Request, state: ApplicationState, error: Exception, token: str, issued: str
) -> Response:
    """The household's file refusing a first upload's write. A busy file wrote nothing, as the
    route says a busy store. For any other refusal whether the write committed can't be
    known: the page says so and offers the upload's form once more, with the token and the
    time the refused upload was sent with, never new ones. Screenshots sent from it go with
    that token, so the store adds them to the review it names, made by the first write or made
    now, or refuses them as it refuses any upload. The form is as old as it was. The token
    names the review and checks no file. Nothing is sent until the files are chosen and the
    button pressed."""
    if busy_refusal(error):
        return upload_refusal(request, state, error)
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "said": [UNKNOWN_SAID["upload"]],
            "again": {"said": TRY_UPLOAD_AGAIN, "token": token, "issued": issued},
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=503,
    )


TYPED_LABELS: Final = {
    "course": "Class",
    "title": "Title",
    "instruction": "Instructions",
    "footer": "Due date",
    "heading": "Date (day heading)",
    "due": "Due date",
    "assigned": "Assigned date",
}
"""How each correction given back is labeled, since the review's cards aren't shown with it."""
TYPED_SAID: Final = (
    "What you typed, as you sent it. This review can't keep it; copy anything you still need."
)
"""What the boxes of typing given back say first."""
UNSHOWN_TYPED: Final = (
    "What you typed, as you sent it. Copy anything you still need before leaving this page."
)
"""What the boxes of typing given back say first where the review can't be shown or a save's outcome
can't be told, beside a confirmed save's counts with no post to read, for what "No assigned date
was given" set aside from an open review, and on Save for later's page after a commit that set
words aside, whether or not another tab saved since."""
REVIEW_NOT_SHOWN: Final = "This review can't be shown right now."
"""What a press's page says after its own lines when the store refuses to read the review."""
REVIEW_UNAVAILABLE: Final = "This review can't be shown right now. Try again in a moment."
"""What the review's address says while the store refuses to read it."""
READINGS_UNCONFIRMED: Final = (
    "Blossom couldn't confirm which screenshot readings from this attempt were kept."
)
"""What a Read press says when the household's file refused it as its attempt began, before any
reading, or as it ended, after its readings were kept: none, some or all may have been."""
NOTHING_ADDED: Final = "Nothing was added."
"""What an upload says when nothing of it was added: its images all refused, or the household's
file refusing a read before its write began."""
UNKNOWN_SAID: Final = {
    "upload": "Blossom couldn't tell whether the screenshots were uploaded.",
    "remove": "Blossom couldn't tell whether the screenshot was taken out of this review.",
    "keep": "Blossom couldn't tell whether your answers were kept.",
    "later": (
        "Blossom couldn't tell whether your answers were kept and your corrections saved for later."
    ),
    "save": "Blossom couldn't tell whether your answers were kept.",
    "homework": "Blossom couldn't tell whether any homework was saved.",
    "set-aside": "Blossom couldn't tell whether this group was set aside.",
    "finish": "Blossom couldn't tell whether this review was finished.",
    "discard": "Blossom couldn't tell whether this review was discarded.",
    "not-hers": 'Blossom couldn\'t tell whether this review was discarded as "Not hers".',
}
"""What each press says when the household's file refused its write for a reason that isn't a
busy file, so whether its change was made isn't known: the change it would have made, and
nothing more. A Save whose answers' keep is refused this way says too that no homework was
saved, since its homework was never begun. A first upload says too how to try again, above
the form that carries its own token and time (``unwritten_upload``)."""
COUNTS_UNSHOWN: Final = "This homework was already saved, but its counts can't be shown right now."
"""What a resent save says when its earlier save is on record and its counts can't be read."""


async def gone_with_typing(
    request: Request,
    state: ApplicationState,
    gone: str,
    said: Sequence[str],
    status: int,
    *,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A press on a review that expired, that the sweep took away or that has closed: what the press
    did first, if anything committed, as an ended review's page says it (``ended_lines``), then why
    the review can't take more, and every correction the post typed in a box to copy, read from the
    post alone by its press (``typing_by_press``), apart from a box this request's own committed
    Save for later is shown to hold exactly (``saved_exactly``). A save that committed answers
    through ``saved_unread`` instead.
    Nothing of the review is shown, nothing is stored, and nothing more can be saved from it."""
    typed = await typing_by_press(
        request, as_sent=True, beside_words=True, saved_exactly=saved_exactly
    )
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "said": [*typing_here(ended_lines(said, saved=False), typed), SAID[gone]],
            "typed": typed,
            "typed_said": TYPED_SAID,
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=status,
    )


async def unknown_outcome(
    request: Request,
    state: ApplicationState,
    kept: Sequence[str] = (),
    homework: Sequence[str] = (UNKNOWN_SAID["homework"],),
) -> Response:
    """A Save press that can't ask whether a save is on record: a transaction left open on the
    record's connection, or a refused read of its acceptance. The page draws its first line as
    its alert, so the lines that report a problem come first. Of what its caller passes
    (``kept``), those are every line that isn't an answers line: that its answers couldn't be
    kept, with no instruction to press again (``with_no_retry``); that it can't tell whether
    they were; or a refused post's own sentence, which says nothing was kept or saved by this
    attempt. Then its homework lines (``homework``). Where its caller's own values show that this
    press wrote no homework, they say that no homework was saved by this attempt and that it
    couldn't tell whether another attempt saved any (``NO_HOMEWORK_BY_THIS_ATTEMPT``), the second
    alone after a refused post's sentence. Every other caller says that it couldn't tell whether
    any homework was saved: a homework save that returned not saved and left a transaction open;
    one whose check for a transaction left open was refused, at a later look that is refused;
    and a refused look at the first reads of ``review_press``, past a post refused for a box it
    can't take, past an answers write refused as out of date, closed, expired or removed, or by
    a file error that isn't busy, past a name check that can't be read, and past "Already
    saved". Last, the answers lines among ``kept`` (``ANSWERS_SAID``), there only where its own
    keep committed, which its caller says as an ended review's page does once a read has said
    the review ended. Then every correction to copy, and the way to Family review. It shows no
    rows or review link, since rows rebuilt here would carry a new acceptance, and a Save from
    them wouldn't be a replay of this press."""
    lines = with_no_retry(kept)
    problems = [line for line in lines if line not in ANSWERS_SAID]
    answers = [line for line in lines if line in ANSWERS_SAID]
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "said": [*problems, *homework, *answers],
            "typed": await typing_of(request, as_sent=True, beside_words=True),
            "typed_said": UNSHOWN_TYPED,
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=503,
    )


async def closed_on_press(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    status: int,
    kept: Sequence[str] = (),
    *,
    rows: bool = True,
    notes: Sequence[str] = (),
    beside_a_receipt: Sequence[str] = (),
    saved_exactly: Collection[tuple[str, str, str]] = (),
    homework: Sequence[str] = (UNKNOWN_SAID["homework"],),
) -> Response:
    """A press on a review closed under it: the outcome of an earlier save of this page when one is
    confirmed, its page showing the review's rows only with ``rows``; on a Save press whose receipt
    can't be read, what it knows of its answers (``kept``), a line it kept as an ended review's
    page says it, then its homework lines (``homework``, as ``unknown_outcome`` says them); else the
    closed review's words and every correction to copy, with nothing that reopens the review or
    offers a press.
    What this press's own write kept, or that it can't tell (``notes``), goes on either as an ended
    review's page says it, apart from where ``saved_page`` leaves a kept line unsaid; a refusal of
    its own (``beside_a_receipt``) goes on beneath an earlier save's outcome only. Either page's
    copies leave out each box this request's own committed Save for later is shown to hold exactly
    (``saved_exactly``)."""
    return await replayed(
        request,
        state,
        draft_id,
        ended_lines(kept, saved=True),
        rows=rows,
        notes=[*ended_lines(notes, saved=True), *beside_a_receipt],
        closed_seen=True,
        saved_exactly=saved_exactly,
        homework=homework,
    ) or await gone_with_typing(
        request, state, "closed", notes, status, saved_exactly=saved_exactly
    )


async def ended_under_press(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    closed_status: int,
    kept: Sequence[str] = (),
    *,
    refused_status: int = 503,
    answered: bool = False,
    corrections: Sequence[str] = (),
    failed: Sequence[str] = (),
    refusal: Sequence[str] = (),
    beside_a_receipt: Sequence[str] = (),
    read_again: list[DraftSnapshot] | None = None,
    saved_exactly: Collection[tuple[str, str, str]] = (),
    homework: Sequence[str] = (UNKNOWN_SAID["homework"],),
) -> Response | None:
    """A press that read its review open and then found no rows or no post to read: the review read
    again, so that one that has closed or gone says so with the typing, and one the store refuses to
    read says that. Each asks first for its receipt, since a copy of the page's Save can have saved
    since its last check and finished the review, which still reads open until it's closed. A Save
    press asks the same of a review that reads open. No other press does: it has no save of its own
    to look for, and an earlier save of its page goes unasked. None when the review reads open and
    no receipt was asked for or found. Any answer to that read but a review reading open gives a
    press other than Save a receipt's page with no rows (``rows``). A refused read with no receipt
    answers ``refused_status``. A press says its answers lines (``kept``, what its own write stored
    or dropped) on every page here, as an ended review's page says them where the read says the
    review ended. One whose write stored answers (``answered``) answers a refused read with no
    receipt at 200, and says its corrections line (``corrections``: Keep's warning, or Save for
    later's refusal as behind) after its answers lines on each page past a refused read and
    beneath a receipt found where the review reads open; a read saying the review ended leaves
    that line out. A Save press that can't read its receipt says what it knows of its answers,
    then its homework lines (``homework``, as ``unknown_outcome`` says them). A press's own
    failed write, or one that can't be told (``failed``), is said on every page here, at the
    status it answers, and beneath a receipt's result, since that result doesn't settle it. Why a
    press's post was refused (``refusal``) is said the same way, and beside a Save's can't-tell
    answer too, since a refused lookup doesn't show there's no earlier save. Save for later's
    refusal as behind (``beside_a_receipt``) goes beneath a receipt's result, after the rest,
    once. Where it gives None, the snapshot it read goes in ``read_again``. Each page it answers
    with leaves out of its copies the boxes this request's own committed Save for later is shown
    to hold exactly (``saved_exactly``)."""
    its_own = [*kept, *(corrections if answered else ()), *failed, *refusal]
    at_an_end = [*kept, *failed, *refusal]
    its_kept = [*kept, *failed, *refusal]
    beneath = list(dict.fromkeys([*its_own, *beside_a_receipt]))
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        now = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except (DraftExpired, DraftNotFound) as error:
        recorded = await replayed(
            request,
            state,
            draft_id,
            ended_lines(its_kept, saved=True),
            rows=await save_pressed(request),
            notes=[*ended_lines(at_an_end, saved=True), *beside_a_receipt],
            saved_exactly=saved_exactly,
            homework=homework,
        )
        return recorded or await refused_keeping_typing(
            request, state, error, at_an_end, saved_exactly=saved_exactly
        )
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        noted(error)
        recorded = await replayed(
            request,
            state,
            draft_id,
            its_kept,
            rows=await save_pressed(request),
            notes=beneath,
            saved_exactly=saved_exactly,
            homework=homework,
        )
        status = 200 if answered else refused_status
        return recorded or await unshown_with_typing(
            request, state, draft_id, its_own, status, as_sent=True, saved_exactly=saved_exactly
        )
    except STORE_REFUSALS:
        recorded = await replayed(
            request,
            state,
            draft_id,
            its_kept,
            rows=await save_pressed(request),
            notes=beneath,
            saved_exactly=saved_exactly,
            homework=homework,
        )
        status = 200 if answered else refused_status
        return recorded or await unshown_with_typing(
            request, state, draft_id, its_own, status, as_sent=True, saved_exactly=saved_exactly
        )
    if now.closed_at is None:
        recorded = (
            await replayed(
                request,
                state,
                draft_id,
                its_kept,
                notes=its_own,
                saved_exactly=saved_exactly,
                homework=homework,
            )
            if await save_pressed(request)
            else None
        )
        if recorded is None and read_again is not None:
            read_again.append(now)
        return recorded
    return await closed_on_press(
        request,
        state,
        draft_id,
        closed_status,
        its_kept,
        rows=await save_pressed(request),
        notes=at_an_end,
        beside_a_receipt=beside_a_receipt,
        saved_exactly=saved_exactly,
        homework=homework,
    )


def copy_label(
    request: Request,
    key: str,
    field: str,
    *,
    by_card: bool,
    drawn: Mapping[str, str] | None = None,
) -> str:
    """How a copy of a box is labeled: by its field, and with ``by_card`` for its card too, as the
    returned form names the card its box is drawn on (``drawn``), else as the review state its
    press's post was read against names it (``cards_named``), where one was."""
    label = TYPED_LABELS[field.partition(":")[0]]
    cards: Mapping[str, str] = getattr(request.state, "typed_cards", None) or {}
    if not by_card:
        return label
    card = (drawn or {}).get(key)
    if card is None:
        card = cards.get(key) or cards.get(key.removeprefix("date:").partition("|")[0])
    return label if card is None else f"{label} for {card}"


def text_boxes(stored: Iterable[tuple[str, str, str]]) -> frozenset[tuple[str, str, str]]:
    """The class, title and instructions boxes among a save's evidence, by key, field and text:
    the part of it handed to a page that draws no form (``saved_exactly``). A date entry stays
    behind, so a date box and words carried beside one come back by their own rules there."""
    return frozenset(box for box in stored if words_box(box[1]))


async def typing_of(
    request: Request,
    stored: Iterable[tuple[str, str, str]] = (),
    shown: Collection[tuple[str, str, str]] = (),
    *,
    beside_words: bool = False,
    as_sent: bool = False,
    drawn: Mapping[str, str] | None = None,
    undrawn_beside_words: bool = False,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> list[tuple[str, str]]:
    """Every box a press's post gives back, labeled, read from the post alone (``typed_for``),
    to give back as it was sent, in boxes to copy, where the review can't keep it. A box is
    left out only when a box on the page holds that exact text (``shown``), or when its save shows
    it stored that exact text in that field (``stored``, the outcome's own evidence, or
    ``saved_exactly``, the class, title and instructions boxes of it that a page drawing no form is
    handed). An empty box or a hidden field beside it isn't either, whatever was ticked. With
    ``beside_words``, a date box beside carried words comes back as ``typed_beside_words`` gives
    it, and with ``undrawn_beside_words`` one no box in ``shown`` draws; with ``as_sent``, each
    text exactly as sent (``exact``), labeled by its card (``copy_label``, with ``drawn``)."""
    try:
        form = await form_of(request)
    except FormUnreadable:
        return []
    items = [(name, value) for name, value in form.multi_items() if isinstance(value, str)]
    evidence = set(stored)
    if beside_words:
        typed = typed_beside_words(items, exact=as_sent)
    elif undrawn_beside_words:
        boxes = {(key, field) for key, field, _ in shown}
        typed = typed_beside_words(items, exact=as_sent, drawn=boxes)
    else:
        typed = typed_for(items, exact=as_sent)
    return [
        (copy_label(request, key, field, by_card=as_sent, drawn=drawn), text)
        for key, field, text in typed
        if (key, field, text) not in evidence
        and (key, field, text) not in shown
        and (key, field, text) not in saved_exactly
    ]


async def typing_by_press(
    request: Request,
    stored: Iterable[tuple[str, str, str]] = (),
    shown: Collection[tuple[str, str, str]] = (),
    *,
    beside_words: bool = False,
    as_sent: bool = False,
    every_press: bool = False,
    drawn: Mapping[str, str] | None = None,
    undrawn_beside_words: bool = False,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> list[tuple[str, str]]:
    """What a post gives back when its press finds the review ended, on every page that says it
    can't be shown, beside a confirmed save's counts with no post to read, and on Save for later's
    page after a commit that set words aside, whether or not its read shows another tab saved
    since: on a Save press, ``typing_of``, with ``beside_words``, ``undrawn_beside_words``,
    ``as_sent`` and ``saved_exactly`` passed on; on Keep or Save for later, every box as it was
    sent, whitespace included (``typed_as_sent``), labeled by its card with ``every_press``
    (``drawn`` as ``copy_label`` takes it). Text a box on the page holds exactly (``shown``) is left
    out, and so is a class, title or instructions box whose exact text this request's own confirmed
    write shows it holds (``saved_exactly``), by key, field and text. A caller that was handed no
    such evidence passes none, and every box comes back."""
    try:
        form = await form_of(request)
    except FormUnreadable:
        return []
    items = [(name, value) for name, value in form.multi_items() if isinstance(value, str)]
    if next((value for name, value in items if name == "press"), "") == "save":
        return await typing_of(
            request,
            stored,
            shown,
            beside_words=beside_words,
            as_sent=as_sent,
            drawn=drawn,
            undrawn_beside_words=undrawn_beside_words,
            saved_exactly=saved_exactly,
        )
    return [
        (copy_label(request, key, field, by_card=every_press, drawn=drawn), text)
        for key, field, text in typed_as_sent(items)
        if (key, field, text) not in shown and (key, field, text) not in saved_exactly
    ]


def tick_set_aside(
    request: Request,
    post: ReviewPost,
    shown: Collection[tuple[str, str, str]] = (),
    drawn: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """What "No assigned date was given" set aside or was refused beside, labeled by its card
    (``drawn`` as ``copy_label`` takes it), as it was sent, apart from what a box on the page holds
    exactly (``shown``)."""
    return [
        (copy_label(request, key, field, by_card=True, drawn=drawn), text)
        for key, field, text in post.dropped
        if (key, field, text) not in shown
    ]


def fields_shown(rows: RowsShown | None) -> frozenset[tuple[str, str, str]]:
    """Each correction box these rows show, by the key and field a post names it with and the text
    it holds, apart from a one-line box holding a line break, which its control drops. With no
    rows, none."""
    if rows is None:
        return frozenset()
    return frozenset(
        (str(key), str(field), box.value)
        for unit in rows.units
        for block in unit.blocks
        for card in block.cards
        for box in card.boxes
        for key, field in [json.loads(box.subject)]
        if box.kind != "text" or not {"\n", "\r"} & set(box.value)
    )


def cards_drawn(rows: RowsShown) -> dict[str, str]:
    """Each key a box of these rows is posted by, with the accessible name of the card it is drawn
    on, as the form words it."""
    drawn: dict[str, str] = {}
    for unit in rows.units:
        for block in unit.blocks:
            for card in block.cards:
                for box in card.boxes:
                    drawn.setdefault(str(json.loads(box.subject)[0]), card.about)
    return drawn


def boxes_as_sent(rows: RowsShown, items: Sequence[tuple[str, str]]) -> RowsShown:
    """These rows with each text box whose value equals, once canonical, what the post sent in it
    holding the text as sent, line endings made line feeds where its box has several lines."""
    sent = {name: value for name, value in reversed(items) if name.startswith("correct:")}

    def box_of(box: Box) -> Box:
        if box.kind == "date" or box.name not in sent:
            return box
        field = str(json.loads(box.subject)[1])
        text = correction_text(field, sent[box.name])
        if canonical(text) != canonical(box.value):
            return box
        return dataclasses.replace(box, value=text)

    return dataclasses.replace(
        rows,
        units=tuple(
            dataclasses.replace(
                unit,
                blocks=tuple(
                    dataclasses.replace(
                        block,
                        cards=tuple(
                            dataclasses.replace(card, boxes=tuple(box_of(b) for b in card.boxes))
                            for card in block.cards
                        ),
                    )
                    for block in unit.blocks
                ),
            )
            for unit in rows.units
        ),
    )


def choices_unlike(post: ReviewPost, rows: RowsShown, no_date_read: Collection[str] = ()) -> bool:
    """Whether a choice the post sent isn't what these rows show checked or, off the page, doesn't
    match the answer read for this response: that answer wins over the signed default, which counts
    only where the read holds none, and an empty choice counts only where it withdraws one. A "No
    assigned date was given" tick off the page matches only where the post carries its piece's day
    heading confirmation, these rows' read holds that answer with the same text, the box's subject
    is in ``no_date_read`` (the post's ``read:`` field beside it is an empty reading's digest), and
    these rows' progress holds no assigned date for the piece."""
    shown: dict[str, frozenset[str]] = {}
    asked = [(q.field, q.options) for q in rows.names]
    asked += [(q.field, q.options) for q in rows.splits]
    for unit in rows.units:
        for block in unit.blocks:
            asked += [(q.field, q.options) for q in block.questions]
            for card in block.cards:
                kinds = [*card.checks, *([] if card.type is None else [card.type])]
                asked += [(q.field, q.options) for q in kinds]
                field = answer_field("destination", card.name)
                shown[field] = frozenset(c.value for c in card.control.choices if c.checked)
                for box in card.boxes:
                    if box.none is not None:
                        shown[box.none[0]] = frozenset({"none"} if box.none[1] else ())
    for field, options in asked:
        shown[field] = frozenset(value for value, _, checked in options if checked)
    withdrew = {answer_field(*key) for key in post.withdrawn if key in rows.held}
    posted = {answer_field(a.kind, a.subject): a for a in post.answers}

    def unlike(field: str, values: tuple[str, ...]) -> bool:
        if field in shown:
            return shown[field] != frozenset(values)
        if not values:
            return field in withdrew
        answer = posted.get(field)
        if answer is not None and (answer.kind, answer.subject) in rows.held:
            return rows.held[(answer.kind, answer.subject)] != answer.value
        if field.startswith("none:"):
            subject = field.removeprefix("none:")
            key = str(json.loads(subject)[0])
            heading = ("confirm", confirm_subject(key.removeprefix("date:"), "heading"))
            confirmation = posted.get(answer_field(*heading))
            return not (
                confirmation is not None
                and rows.held.get(heading) == confirmation.value
                and subject in no_date_read
                and "assigned" not in rows.progress.get(key, {})
            )
        piece = field.removeprefix(answer_field("destination", ""))
        return piece == field or values != (post.defaults.get(piece),)

    return any(unlike(field, values) for field, values in post.choices.items())


async def unshown_with_typing(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    said: Sequence[str],
    status: int,
    *,
    as_sent: bool = False,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A press whose review the store refuses to read: the press's own lines, which say what it
    kept or failed to keep, its kept answers apart from the rest, then that the review can't be
    shown, every correction it typed given back to copy, by its press (``typing_by_press``), and the
    way back to the review. It reads no store and offers nothing that repeats the press; its status
    is the press's own. ``as_sent`` goes to ``typing_by_press``, with the words-aware reading, and
    so does ``saved_exactly``, the boxes this request's own committed Save for later is shown to
    hold exactly, which the copies leave out."""
    typed = await typing_by_press(
        request, as_sent=as_sent, beside_words=as_sent, saved_exactly=saved_exactly
    )
    lines = [
        shown for line in typing_here(said, typed) for shown in UNSHOWN_SAID.get(line, (line,))
    ]
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "kept": apart_lines(lines),
            "said": [*(line for line in lines if line not in APART_SAID), REVIEW_NOT_SHOWN],
            "typed": typed,
            "typed_said": UNSHOWN_TYPED,
            "review": f"{PREFIX}/{draft_id}",
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=status,
    )


def gone_by(error: Exception) -> str:
    """Which of the review's endings a store's refusal names: ``expired``, or ``absent``."""
    return "expired" if isinstance(error, DraftExpired) else "absent"


async def saved_unread(
    request: Request,
    state: ApplicationState,
    done: Sequence[str],
    said: Sequence[str] = (),
    week: str | None = None,
    *,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A save that committed whose review the store refuses to read: what it saved, in the
    success panel whose heading takes the focus, with what else the press said beneath it and
    no word that the review is finished, since that isn't known, and every correction the press
    typed given back to copy, by its press (``typing_by_press``), apart from the boxes this
    request's own committed write is shown to hold exactly (``saved_exactly``)."""
    typed = await typing_by_press(
        request, as_sent=True, beside_words=True, saved_exactly=saved_exactly
    )
    return saved_outcome(request, state, done, week, finished=False, typed=typed, notes=said)


async def saved_busy(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    done: Sequence[str],
    said: Sequence[str] = (),
    week: str | None = None,
    *,
    notes: Sequence[str] = (),
    as_sent: bool = False,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A save that committed whose review the store refuses to read: what it saved in the
    success panel, whose heading takes the focus, with ``notes`` beneath it; then, in the problem
    style, what else the press said as a page without the rows says it (``UNSHOWN_SAID``) and that
    the review can't be shown; every correction it typed given back to copy, by its press
    (``typing_by_press``, with ``as_sent`` and then the words-aware reading), apart from the boxes
    this request's own committed write is shown to hold exactly (``saved_exactly``); and the way
    back to the review. Where a line of ``notes`` says this press kept answers before the review
    ended, a read of this press has said that it ended, so the counts say what was left as it
    stood at the save (``counts_then``); with no such line they are said as the review says
    them."""
    lines = [shown for line in said for shown in UNSHOWN_SAID.get(line, (line,))]
    if KEPT_BEFORE_IT_ENDED.intersection(notes):
        done = [counts_then(line) for line in done]
    return saved_outcome(
        request,
        state,
        done,
        week,
        finished=False,
        typed=await typing_by_press(
            request, as_sent=as_sent, beside_words=as_sent, saved_exactly=saved_exactly
        ),
        notes=notes,
        problems=[*lines, REVIEW_NOT_SHOWN],
        typed_said=UNSHOWN_TYPED,
        review=f"{PREFIX}/{draft_id}",
    )


async def refused_keeping_typing(
    request: Request,
    state: ApplicationState,
    error: Exception,
    said: Sequence[str] = (),
    status: int | None = None,
    *,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A store's refusal of a press, as its page says it: for a review that expired, or that the
    sweep has since removed, with the press's typing given back, after what the press did if
    anything of it committed; any other as it always is, its own sentence first as the page's
    alert, then the press's answers lines, which a refusal saying the review closed gets as an
    ended review's page says them. The typing given back leaves out the boxes this request's own
    committed Save for later is shown to hold exactly (``saved_exactly``)."""
    if isinstance(error, DraftExpired):
        return await gone_with_typing(
            request, state, "expired", said, status or 410, saved_exactly=saved_exactly
        )
    if isinstance(error, DraftNotFound):
        return await gone_with_typing(
            request, state, "absent", said, status or 404, saved_exactly=saved_exactly
        )
    answers = kept_lines(said)
    if isinstance(error, DraftClosed):
        answers = ended_lines(answers, saved=True)
    return store_refusal(request, state, error, answers)


async def review_state_now(state: ApplicationState, draft_id: str) -> DraftSnapshot | str:
    """The review as one read sees it, or why it can't say: it has expired (``expired``), is gone
    (``absent``), or the store or the file refuses the read (``busy``)."""
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        return await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except DraftExpired:
        return "expired"
    except DraftNotFound:
        return "absent"
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        noted(error)
        return "busy"
    except STORE_REFUSALS:
        return "busy"


async def snapshot_keeping_typing(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    said: Sequence[str] = (),
    status: int | None = None,
    done: Sequence[str] = (),
    saved: Saved | None = None,
    stored: Sequence[str] = (),
    save_press: bool = False,
    *,
    saved_exactly: Collection[tuple[str, str, str]] = (),
    homework: Sequence[str] = (UNKNOWN_SAID["homework"],),
) -> DraftSnapshot | Response:
    """The review as one read sees it after a press, or the page that says why it can't be
    shown: for a review that expired or is gone, what the press did and its typing. After a
    save that committed (``done``, what it saved; ``saved``, its outcome), every answer keeps
    what it saved and the typing. A review the store refuses to read, for any reason,
    answers with that the review can't be shown and the way back to it (``saved_busy``), never
    that nothing was saved; one known to have expired, or to be gone, says so beneath what it
    saved (``saved_unread``); one found closed, as another tab can leave it, answers as a finished
    review does (``saved_finished``). Each of those pages says the answers lines of that save's own
    write (``stored``) beneath its result, as an ended review's page says them where the read says
    the review ended. On any press with no save committed a review found closed answers with its
    typing to copy (``closed_on_press``), a receipt's page showing rows only on a Save press, after
    the press's answers lines and any line saying its own write failed or can't be told, as an
    ended review's page says them. Any other press the
    store refuses to read for answers with
    its own lines and status (``unshown_with_typing``). A file that refuses the read for any
    reason answers as a busy store does. A Save press with no save committed (``save_press``) asks
    for its receipt before any page that ends it, since another copy of the press can have saved
    since its last check, and a receipt it finds says the press's own lines too; where that look
    is refused it says its homework lines (``homework``, as ``unknown_outcome`` says them); a
    commit after that last look goes unseen. After a save that committed, a page that can't read
    the review leaves out of its copies the class, title and instructions boxes that save is
    shown to hold exactly (``saved_exactly``)."""
    press_kept = [*kept_lines(said), *own_failure_lines(said)]
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except (DraftExpired, DraftNotFound) as error:
        if done:
            ended = [*ended_lines(said, saved=True), SAID[gone_by(error)]]
            lines = [*ended_lines(done, saved=True), *ended_lines(stored, saved=True)]
            return await saved_unread(request, state, lines, ended, saved_exactly=saved_exactly)
        recorded = (
            await replayed(
                request,
                state,
                draft_id,
                ended_lines(press_kept, saved=True),
                notes=ended_lines(press_kept, saved=True),
                homework=homework,
            )
            if save_press
            else None
        )
        return recorded or await refused_keeping_typing(request, state, error, said, status)
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        noted(error)
        looks = save_press and not done
        recorded = (
            await replayed(
                request, state, draft_id, press_kept, notes=press_kept, homework=homework
            )
            if looks
            else None
        )
        return recorded or await unshown(
            request, state, draft_id, said, status, done, notes=stored, saved_exactly=saved_exactly
        )
    except STORE_REFUSALS as error:
        if done:
            return await unshown(
                request,
                state,
                draft_id,
                said,
                status,
                done,
                notes=stored,
                saved_exactly=saved_exactly,
            )
        recorded = (
            await replayed(
                request, state, draft_id, press_kept, notes=press_kept, homework=homework
            )
            if save_press
            else None
        )
        return recorded or await refused_keeping_typing(request, state, error, said, status)
    if snapshot.closed_at is not None:
        if done:
            pairs, evidence = (saved.pairs, saved.stored) if saved else ((), ())
            return await saved_finished(
                request,
                state,
                done,
                said,
                pairs,
                evidence,
                closed=True,
                notes=ended_lines(stored, saved=True),
            )
        return await closed_on_press(
            request,
            state,
            draft_id,
            200,
            press_kept,
            rows=save_press,
            notes=press_kept,
            beside_a_receipt=behind_lines(said),
            homework=homework,
        )
    return snapshot


async def unshown(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    said: Sequence[str],
    status: int | None,
    done: Sequence[str] = (),
    *,
    notes: Sequence[str] = (),
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A press whose review can't be read: after a save that committed, what it saved, with
    ``notes`` beneath it and the boxes in ``saved_exactly`` left out of its copies
    (``saved_busy``); else its own lines and status (``unshown_with_typing``)."""
    if done:
        return await saved_busy(
            request,
            state,
            draft_id,
            done,
            said,
            notes=notes,
            as_sent=True,
            saved_exactly=saved_exactly,
        )
    return await unshown_with_typing(request, state, draft_id, said, status or 503, as_sent=True)


STORE_REFUSALS: Final = (
    TooManyImages,
    DraftClosed,
    DraftExpired,
    DraftFrozen,
    DraftNotFound,
    StoreBusy,
    WriterBusy,
)
STORE_FAILURES: Final = (sqlite3.Error, WriterBusy, *STORE_REFUSALS)
"""What a store can answer a read with, as against an error in Blossom's own code."""


@router.post("")
async def first_upload(request: Request, state: State) -> Response:
    """A first upload: the review its form's token names, made now when there is none."""
    try:
        upload = await body_of(request, FIRST_FIELDS)
    except UploadRefused as refusal:
        return refused_upload(request, state, refusal)
    fields = upload.fields
    token = fields.get("token")
    in_time = issued_in_time(state, fields.get("issued"))
    if token is None or not opaque(token) or in_time is None:
        return said_page(request, marks_of(state), UPLOAD_SAID["unreadable"], 400)
    if not in_time:
        return said_page(request, marks_of(state), SAID["too_old"], 400)
    prepared = await asyncio.to_thread(prepare_upload, upload.files)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
    except sqlite3.Error as error:
        noted(error)
        return unread_page(request, state, error, upload=True)
    try:
        added = await asyncio.to_thread(
            state.import_drafts.create,
            student,
            kind=HOMEWORK,
            made_by=role_of(state),
            creation_token=token,
            images=[image for _, image in prepared.images],
        )
    except STORE_REFUSALS as error:
        return upload_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return unwritten_upload(request, state, error, token, fields["issued"])
    if added is None:
        lines = [image_refusal(place + 1, kind) for place, kind in prepared.refused]
        return said_page(request, marks_of(state), NOTHING_ADDED, 422, *lines)
    places = [place for place, _ in prepared.images]
    told = await upload_told(state, added)
    return to_review(state, added.draft_id, upload_said(added, places, prepared.refused), told)


async def upload_told(state: ApplicationState, added: Added) -> list[str]:
    """What an upload says it added, beside how many screenshots the review holds and how many
    are read, as the review stands after its commit."""
    if not added.added:
        return []
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        after = await asyncio.to_thread(state.import_drafts.snapshot, student, added.draft_id)
    except (*STORE_REFUSALS, sqlite3.Error) as error:
        noted(error)
        return []
    read = sum(1 for image in after.images if image.read)
    return uploaded_said(len(added.added), len(after.images), read)


@router.post("/{draft_id}/images")
async def more_images(request: Request, draft_id: str, state: State) -> Response:
    """Add an upload's images to an open review, with no version comparison; the review is
    checked before any byte of the body is read, and again when the images are stored."""
    if not opaque(draft_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
        if snapshot.closed_at is not None or snapshot.finished:
            raise DraftClosed
        if snapshot.frozen:
            raise DraftFrozen
    except STORE_REFUSALS as error:
        return upload_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return unread_page(request, state, error, upload=True)
    try:
        upload = await body_of(request, {})
    except UploadRefused as refusal:
        return refused_upload(request, state, refusal)
    prepared = await asyncio.to_thread(prepare_upload, upload.files)
    try:
        added = await asyncio.to_thread(
            state.import_drafts.add_images,
            student,
            draft_id,
            [image for _, image in prepared.images],
        )
    except STORE_REFUSALS as error:
        return upload_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return unwritten_page(request, state, error, UNKNOWN_SAID["upload"], upload=True)
    places = [place for place, _ in prepared.images]
    told = await upload_told(state, added)
    return to_review(state, draft_id, upload_said(added, places, prepared.refused), told)


async def tables_of(state: ApplicationState, draft_id: str) -> DraftForSave | None:
    """A homework review's tables as the save reads them; None for any other review."""
    record = state.project_state
    student = await asyncio.to_thread(record.student_id)
    return await asyncio.to_thread(record.draft_for_save, student, draft_id)


async def change_lines(
    state: ApplicationState, draft_id: str, before: DraftForSave | None
) -> list[str]:
    """What the page after a change of screenshots says of the answers it cleared and the rows
    left unsaved it kept or asks about again, from the review's tables before the change and
    its readings after; nothing when the review isn't open homework on both sides, or when the
    household's file refuses the read after the change."""
    if before is None or before.closed_at is not None:
        return []
    try:
        after = await tables_of(state, draft_id)
    except sqlite3.Error as error:
        noted(error)
        return []
    if after is None or after.closed_at is not None:
        return []
    answers = {key: value for key, (value, _) in before.answers.items()}
    return change_said(change_told(before.readings, after.readings, answers, before.discards))


@router.post("/{draft_id}/images/{image_id}/remove")
async def remove_image(request: Request, draft_id: str, image_id: str, state: State) -> Response:
    """Remove one image and its reading, with no version comparison; nothing of a body is read.
    The next page says what the change cleared."""
    if not opaque(draft_id) or not opaque(image_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        before = await tables_of(state, draft_id)
    except sqlite3.Error as error:
        noted(error)
        return unread_page(request, state, error)
    try:
        await asyncio.to_thread(state.import_drafts.remove_image, student, draft_id, image_id)
    except UnknownImage:
        return to_review(state, draft_id, [SAID["image_gone"]])
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return unwritten_page(request, state, error, UNKNOWN_SAID["remove"])
    return to_review(state, draft_id, await change_lines(state, draft_id, before))


@router.get("/{draft_id}/images/{image_id}")
async def view_image(request: Request, draft_id: str, image_id: str, state: State) -> Response:
    """The prepared PNG of one image: the bytes stored, sent to the reader and shown."""
    if not opaque(draft_id) or not opaque(image_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        png = await asyncio.to_thread(state.import_drafts.image_png, student, draft_id, image_id)
    except UnknownImage:
        return said_page(request, marks_of(state), SAID["image_gone"], 404)
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return unread_page(request, state, error)
    return Response(png, media_type="image/png")


# ------------------------------------------------------------------ the review and a Read press


def read_check(page_key: bytes, image_ids: Sequence[str]) -> str:
    """The signature of the image ids a Read form names, under the review's own page key."""
    return hmac.new(page_key, ",".join(image_ids).encode(), hashlib.sha256).hexdigest()


def until_text(state: ApplicationState, moment: datetime) -> str:
    """An instant as the review says it, in the household's zone: "Friday, October 16, 9:41 PM"."""
    local = moment.astimezone(state.clock.zone)
    hour = local.hour % 12 or 12
    half = "AM" if local.hour < 12 else "PM"
    return f"{local:%A}, {local:%B} {local.day}, {hour}:{local:%M} {half}"


def label_of(snapshot: DraftSnapshot, index: int) -> str:
    """How one image of the review stands, in ``LABELS`` and ``FAILED``'s words."""
    image = snapshot.images[index]
    if image.read:
        kinds = {reading.sha256: reading.kind for reading in snapshot.readings}
        return (
            LABELS["read"] if kinds.get(image.sha256) == "homework_list" else LABELS["wrong_kind"]
        )
    if image.failure is not None:
        return FAILED[image.failure]
    if image.in_ended_attempt:
        return LABELS["ended"]
    return LABELS["unread"]


def picked(progress: str | None) -> frozenset[str] | None:
    """The rows a page load ticks: those saved progress names when there is saved progress,
    else None, so every group ready to save is ticked."""
    read = progress_read(progress)
    return None if read is None else read.selected


async def saved_corrections(
    state: ApplicationState,
    snapshot: DraftSnapshot,
) -> dict[str, dict[str, str | None]]:
    """The corrections a review's saved progress keeps, in the shape a press posts them:
    progress of shape 3 read as the boxes its corrections give, so its answers and "Not saved
    yet" are worked out against what the page posts, and its card's destination asks again
    once, not after every press."""
    read = progress_read(snapshot.progress)
    if read is None or read.shape == PROGRESS_SHAPE or not read.corrected:
        return {} if read is None else read.corrected
    review = await state_of_review(state, snapshot, read.corrected)
    return {} if review is None else shape_four(review, read.corrected)


def row_keys(review: ReviewState, units: Iterable[str]) -> frozenset[str]:
    """The row keys of the groups named, as saved progress keeps a selection."""
    return frozenset(
        review.by_name[name].key
        for unit in units
        for name in unit.split("+")
        if name in review.by_name
    )


async def image_names(
    request: Request, state: ApplicationState, snapshot: DraftSnapshot
) -> tuple[bytes, list[ImageName]] | None:
    """The key name forms are made under, and what each read image's student line is against
    her record; None when the household secret the key comes from can't be read."""
    places = {image.sha256: index + 1 for index, image in enumerate(snapshot.images)}
    ids = {image.sha256: image.image_id for image in snapshot.images}
    try:
        key = await grade_name_key(request, state)
    except (UnreadableHouseholdSecret, UnsafeCheckpointPath, OSError) as error:
        logger.warning("The household secret couldn't be read: %s", type(error).__name__)
        return None
    images = []
    for reading in snapshot.readings:
        if reading.kind != "homework_list" or reading.sha256 not in ids:
            continue
        line = student_line(reading.body)
        record = state.project_state
        identity = await asyncio.to_thread(record.settled, record.identity_of, key, line)
        images.append(
            ImageName(
                ids[reading.sha256],
                places[reading.sha256],
                reading.sha256,
                identity.status.value,
                identity.form,
                line,
            )
        )
    return key, images


def as_kept(tables: DraftForSave, post: ReviewPost) -> DraftForSave:
    """The tables with a press's answers held as keeping them would hold them, worked out in
    memory and never stored: an answer for pieces another save accepted is left out, each other
    answer replaces the one held, and each answer the page asked for and the press left blank is
    taken back. On a page other than Review changes, an answer that changes one the draft holds is
    left out too: that page gives it back as posted, "Not saved yet." beside it. A split answer is
    never held: day cards are drawn only from a stored split, and its question keeps the answer."""
    answers = dict(tables.answers)
    posted = {
        (answer.kind, answer.subject): (answer.value, answer.digest)
        for answer in post.answers
        if answer.kind != "split"
        and not (answer.names and all(name in tables.saved for name in answer.names))
        and (
            post.press == "review"
            or answers.get((answer.kind, answer.subject), (answer.value,))[0] == answer.value
        )
    }
    for key in post.withdrawn:
        if key not in posted:
            answers.pop(key, None)
            if key[0] == "joined":
                # A joined note's tick left blank stands on a page that stores nothing.
                answers[key] = (UNJOINED, "")
    return tables._replace(answers={**answers, **posted})


async def state_of_review(
    state: ApplicationState,
    snapshot: DraftSnapshot,
    corrections: Corrections,
    posted: ReviewPost | None = None,
    defaults_shown: Mapping[str, str] | None = None,
    withheld: frozenset[str] = frozenset(),
) -> ReviewState | None:
    """The review's state as the save works it out, from its stored tables and the record now;
    None when there is no such homework review, or it has closed, as the tables say whatever an
    older read of them said. With ``posted``, that press's answers are held as keeping them
    would hold them, nothing stored (``as_kept``); with ``defaults_shown``, the page defaults it
    sent unchanged are worked out as sent where its corrections withdraw them (``review_state``);
    the pieces in ``withheld`` never start on a new slot.
    Each read of the record is ``settled``: with a transaction left open it refuses
    (``TransactionLeftOpen``), so that isn't taken for an empty or closed review."""
    record = state.project_state
    student = await asyncio.to_thread(record.student_id)
    tables = await asyncio.to_thread(
        record.settled, record.draft_for_save, student, snapshot.draft_id
    )
    if tables is None or tables.closed_at is not None:
        return None
    if posted is not None:
        tables = as_kept(tables, posted)
    return await asyncio.to_thread(
        record.settled,
        review_state,
        record,
        tables,
        now=state.real_clock.now(),
        corrections=corrections,
        defaults_shown=defaults_shown,
        withheld=withheld,
    )


async def rows_of(
    request: Request,
    state: ApplicationState,
    snapshot: DraftSnapshot,
    said: list[str],
    typed: Corrections | None = None,
    ticked: Sequence[str] | None = None,
    both: Sequence[str] = (),
    choices: Mapping[str, tuple[str, ...]] | None = None,
    conflicted: Mapping[str, tuple[str, ...]] | None = None,
    revision: int | None = None,
    own: frozenset[str] = frozenset(),
    answers_read: list[tuple[Mapping[tuple[str, str], str], Corrections] | None] | None = None,
    destinations: Mapping[str, str] | None = None,
    sent_twice: frozenset[str] = frozenset(),
    unresolved: Mapping[str, tuple[str, ...]] | None = None,
    twice: Mapping[str, tuple[str, ...]] | None = None,
    opened: frozenset[str] = frozenset(),
    signed_digests: Mapping[str, tuple[str, ...]] | None = None,
    shown_signed: Mapping[str, tuple[str, ...]] | None = None,
    posted: ReviewPost | None = None,
    unchosen: frozenset[str] = frozenset(),
    retaken: frozenset[str] = frozenset(),
    chosen_here: frozenset[str] = frozenset(),
    left_off: frozenset[str] = frozenset(),
    held_back: frozenset[str] = frozenset(),
) -> RowsShown | None:
    """The review's rows once anything is read, worked out from its stored state and the record
    as the save works them out, with the corrections and the groups ticked as saved for later
    or, when a press's typing comes back, as it sent them; None before anything is read. A press
    with an instruction conflict, or naming a kept row that isn't on the record, comes back with its
    choices as posted (``choices``, ``conflicted``) and signed with its own progress revision
    (``revision``): saving for later in another tab moves that with no change of version, and a
    fresh one would let the next Save for later overwrite that tab's progress unseen. With a
    transaction left open the reads refuse, so rows are never made from what it holds. What its read
    of the review finds the answers to be, with the corrections saved progress keeps as worked out
    here, goes in ``answers_read``, None with no review. Destinations an offer or a found row posted
    (``destinations``) the rows don't select are said (``RowsShown.unselected``). A joined note's
    tick a refused press sent twice with different answers (``sent_twice``) is marked there, and
    one sent twice or marked so and left unanswered (``unresolved``) is offered as its actual
    choice, neither side chosen, until it is answered. A box or answer of one value it sent twice
    with different values (``twice``) names both at that field. The cards in ``opened`` show their
    boxes open, and ``signed_digests`` are signed in place of the groups' own, with ``shown_signed``
    in place of the corrections the page shows (``rows_shown``).
    With ``posted`` (a page that stores nothing), the proposal is worked out with those answers
    held as keeping them would hold them, nothing stored, so every question they raise is shown;
    on a page other than Review changes, with the page defaults it sent unchanged worked out as
    sent where its corrections withdraw them (``destinations_held``). The cards a press left
    unchosen on a page that asked (``unchosen``) and those its typing took back (``retaken``)
    never start on a new slot, and the latter say why they ask again. A press that gives its
    choices back selects the group of each card whose destination it chose (``chosen_here``),
    unless it unticked that group (``left_off``). The cards a Save held back (``held_back``) wait
    for Review changes."""
    if not any(r.kind == "homework_list" for r in snapshot.readings):
        return None
    kept = await saved_corrections(state, snapshot)
    corrections = kept if typed is None else typed
    shown_defaults = None if posted is None or posted.press == "review" else posted.defaults
    review = await state_of_review(
        state, snapshot, corrections, posted, shown_defaults, unchosen | retaken
    )
    if answers_read is not None:
        answers_read.append(None if review is None else (review.held, kept))
    open_keys = [row.key for row in snapshot.rows if row.state == "open"]
    items_open = any(not row.readable and row.state == "open" for row in snapshot.rows)
    if review is None or (not review.open and not review.asking and not items_open):
        return None
    unread = homework_assembly(snapshot.readings, now=state.real_clock.now()).unread
    named = await image_names(request, state, snapshot)
    if named is None:
        said.append(SAID["no_name_check"])
    return await asyncio.to_thread(
        state.project_state.settled,
        rows_shown,
        review,
        places={image.sha256: index + 1 for index, image in enumerate(snapshot.images)},
        images=[] if named is None else named[1],
        page_key=snapshot.page_key,
        version=snapshot.version,
        progress_revision=snapshot.progress_revision if revision is None else revision,
        acceptance=secrets.token_hex(16),
        until=until_text(state, snapshot.expires_at),
        corrections=corrections,
        selected=row_keys(review, ticked) if ticked is not None else picked(snapshot.progress),
        unsaved=frozenset() if typed is None else unsaved_typing(snapshot, typed, kept),
        open_keys=open_keys,
        unread=unread,
        frozen=snapshot.frozen,
        image_ids={image.sha256: image.image_id for image in snapshot.images},
        href=f"{PREFIX}/{snapshot.draft_id}",
        both=frozenset(both),
        choices=choices,
        conflicted=conflicted,
        own=own,
        progress=kept,
        destinations=destinations,
        sent_twice=sent_twice,
        unresolved=unresolved,
        twice=twice,
        opened=opened,
        signed_digests=signed_digests,
        shown_signed=shown_signed,
        retaken=retaken,
        chosen_here=chosen_here,
        left_off=left_off,
        held_back=held_back,
    )


def review_page(
    request: Request,
    state: ApplicationState,
    snapshot: DraftSnapshot,
    said: Sequence[str] = (),
    status: int = 200,
    rows: RowsShown | None = None,
    told: Sequence[str] = (),
    done: Sequence[str] = (),
    typed: Sequence[tuple[str, str]] = (),
    typed_said: str = TYPED_SAID,
) -> Response:
    """The review: what a press did and found wrong, each in its own style, what it typed that has
    no box here and wasn't shown stored, to copy (``typed``, under ``typed_said``), its images with
    their labels and how many are uploaded and read, those read as her homework list marked so
    the page can fold them away, the Read press for the unread ones when a key is set and no
    attempt is current, the form that adds more, "Discard the review" whatever is read, and
    once anything is read, the rows with their cards, questions and what saving adds."""
    keyed = model_configured(state.settings)
    unread = [image.image_id for image in snapshot.images if not image.read]
    homework_lists = {
        reading.sha256 for reading in snapshot.readings if reading.kind == "homework_list"
    }
    ends = snapshot.attempt_ends
    when = None
    if ends is not None:
        left = max(0.0, ends - state.monotonic())
        when = until_text(state, state.real_clock.now() + timedelta(seconds=left))
    return templates.TemplateResponse(
        request,
        "import_review.html",
        {
            "marks": marks_of(state),
            "page": "parent",
            "draft_id": snapshot.draft_id,
            "until": until_text(state, snapshot.expires_at),
            "images": [
                {
                    "id": image.image_id,
                    "place": index + 1,
                    "label": label_of(snapshot, index),
                    "read": image.read and image.sha256 in homework_lists,
                }
                for index, image in enumerate(snapshot.images)
            ],
            "said": list(said),
            "told": list(told),
            "done": list(done),
            "typed": list(typed),
            "typed_said": typed_said,
            "counts": (
                f"{len(snapshot.images)} uploaded \u00b7 "
                f"{sum(1 for image in snapshot.images if image.read)} read"
            ),
            "read_label": f"Read {len(unread)} screenshot{'' if len(unread) == 1 else 's'}",
            "reading": READING,
            "under_way": None if when is None else under_way(when),
            "read_ids": (
                ",".join(unread)
                if keyed and unread and when is None and not snapshot.frozen
                else None
            ),
            "frozen": SAID["frozen"] if snapshot.frozen else None,
            "discard": signed_action("discard", snapshot.page_key, snapshot.version),
            "read_check": read_check(snapshot.page_key, unread),
            "no_key": None if keyed else SAID["no_key"],
            "disclosed": DISCLOSED,
            "limits": LIMITS,
            "full": len(snapshot.images) >= MOST_IMAGES_PRESENT,
            "rows": rows,
        },
        status_code=status,
    )


async def snapshot_or_page(
    request: Request, state: ApplicationState, draft_id: str, *, address: bool = False
) -> DraftSnapshot | Response:
    """The review as one read sees it, or the page that says why it can't be shown. A busy
    store refuses the review's own address (``address``) with a page saying so, 503, and a
    press with the page saying it changed nothing. A file that refuses the read for any other
    reason is said as the address's page says a busy store."""
    if not opaque(draft_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        noted(error)
        if address:
            return said_page(request, marks_of(state), REVIEW_UNAVAILABLE, 503)
        return unread_page(request, state, error)
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
    if snapshot.closed_at is not None:
        return said_page(request, marks_of(state), SAID["closed"], 200)
    return snapshot


@router.get("/{draft_id}")
async def review(request: Request, draft_id: str, state: State) -> Response:
    """The review: its images, how each stands, and the Read press for those not read."""
    found = await snapshot_or_page(request, state, draft_id, address=True)
    if isinstance(found, Response):
        return found
    note = note_read(state.result_key, draft_id, request.cookies.get(NOTE_COOKIE))
    said = note.said
    if said or note.told or note.done:
        answering(request)
    try:
        rows = await rows_of(request, state, found, said)
    except sqlite3.Error as error:
        noted(error)
        return said_page(request, marks_of(state), REVIEW_UNAVAILABLE, 503)
    answer = review_page(request, state, found, said, rows=rows, told=note.told, done=note.done)
    if request.cookies.get(NOTE_COOKIE) is not None:
        answer.delete_cookie(NOTE_COOKIE, path=f"{PREFIX}/{draft_id}")
    return answer


@router.post("/{draft_id}/read")
async def read(request: Request, draft_id: str, state: State) -> Response:
    """A Read press over the unread images its page showed, signed with the review's key; it
    returns by the attempt's deadline with the review as it stands then, saying what the
    readings stored by then cleared."""
    found = await snapshot_or_page(request, state, draft_id)
    if isinstance(found, Response):
        return found
    fields, whole = await fields_of(request, frozenset({"images", "check"}))
    if not model_configured(state.settings):
        return review_page(request, state, found, [SAID["no_key"]], 409)
    named = [part for part in fields.get("images", "").split(",") if part]
    if (
        not whole
        or not named
        or not all(opaque(part) for part in named)
        or not same_signature(fields.get("check", ""), read_check(found.page_key, named))
    ):
        return review_page(request, state, found, [SAID["unchecked"]], 400)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        before = await tables_of(state, draft_id)
    except sqlite3.Error as error:
        noted(error)
        if busy_file(error):
            return review_page(request, state, found, [SAID["read_busy"]], 503)
        return said_page(request, marks_of(state), REVIEW_UNAVAILABLE, 503)
    try:
        outcome = await read_screenshots(
            state.import_drafts,
            state.settings,
            student,
            draft_id,
            named,
            monotonic=state.monotonic,
            held=state.detached,
        )
    except AttemptUnderWay:
        outcome = None
    except (StoreBusy, WriterBusy):
        return review_page(request, state, found, [SAID["read_busy"]], 503)
    except (DraftClosed, DraftExpired, DraftFrozen, DraftNotFound) as error:
        return store_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        # Refused as the attempt began or as it ended, after its readings may have been kept.
        return to_review(state, draft_id, [READINGS_UNCONFIRMED])
    try:
        after = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        noted(error)
        return to_review(state, draft_id, told=kept_readings(outcome))
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
    if after.closed_at is not None:
        return said_page(request, marks_of(state), SAID["closed"], 200)
    changed = await change_lines(state, draft_id, before)
    said = list(changed)
    told: list[str] = []
    if outcome is not None:
        places = {image.image_id: index + 1 for index, image in enumerate(after.images)}
        read_now = {image.image_id for image in after.images if image.read}
        said += [
            busy_reading(places[image_id])
            for image_id in outcome.busy
            if image_id in places and image_id not in read_now
        ]
        told = read_said(after, state.real_clock.now())
    try:
        rows = await rows_of(request, state, after, said)
    except sqlite3.Error as error:
        noted(error)
        # The review may change before the way back is followed, so its note names no screenshot
        # by its place and gives no total: what this press changed, and its confirmed readings.
        return to_review(state, draft_id, changed, kept_readings(outcome))
    return review_page(request, state, after, said, rows=rows, told=told)


def kept_readings(outcome: ReadOutcome | None) -> list[str]:
    """What a Read press's way back to its review says of its readings when the review can't be
    read again, or its rows built, after its attempt: how many readings the attempt is known to
    have kept, by count alone, and nothing when none is confirmed. An unfinished reading may
    have been kept too, so the count is what was confirmed, not all that was kept. It names no
    screenshot and says nothing of the others; the review's labels, shown beside it, say how
    each one stands."""
    kept = 0 if outcome is None else len(outcome.read)
    if not kept:
        return []
    s = "" if kept == 1 else "s"
    return [f"Blossom confirmed that this attempt kept the reading{s} of {kept} screenshot{s}."]


def read_said(snapshot: DraftSnapshot, now: datetime) -> list[str]:
    """What a Read press says the review's screenshots read as: how many are read as homework
    lists, the assignments found on their cards and the teacher's notes to check, and how many
    are read as something else. Every note is a note to check, whether it reads as homework, as
    unclear, or as no homework, and whether it is a row of its own or joins a card's row."""
    kinds = {reading.sha256: reading.kind for reading in snapshot.readings}
    read = [image for image in snapshot.images if image.read]
    lists = sum(1 for image in read if kinds.get(image.sha256) == "homework_list")
    out = []
    if lists:
        rows = homework_assembly(snapshot.readings, now=now).rows
        own = sum(1 for row in rows if row.reading.teacher_note is not None)
        joined = sum(len(row.reading.joined_notes) for row in rows)
        out.append(read_said_line(lists, len(rows) - own, own + joined))
    if len(read) > lists:
        out.append(not_a_list_line(len(read) - lists))
    return out


def read_said_line(lists: int, found: int, notes: int = 0) -> str:
    """ "1 screenshot read: 3 assignments found.", or with notes to check, "1 screenshot read:
    2 assignments found, 1 note to check."."""
    checks = f", {notes} note{'' if notes == 1 else 's'} to check" if notes else ""
    return (
        f"{lists} screenshot{'' if lists == 1 else 's'} read: "
        f"{found} assignment{'' if found == 1 else 's'} found{checks}."
    )


def not_a_list_line(count: int) -> str:
    """ "1 screenshot read: not a homework list."."""
    return f"{count} screenshot{'' if count == 1 else 's'} read: not a homework list."


# ------------------------------------------------------------------ the review's presses

OUT_OF_DATE: Final = "This page is out of date. Check the review before trying again."
"""What a press says when the review moved on since its page: Keep these answers, Save selected
homework and Save review for later, a Save for later another tab saved past since its own
commit, and the small presses that set aside or discard, each before its own result. It names no
cause: the version says that the review moved, never what moved it."""
NOTHING_BY_THIS_ATTEMPT: Final = "Nothing was kept or saved by this attempt."
"""A press whose post doesn't match its page, where the review it would go back to can't be shown,
has ended, or sits beside an earlier save: nothing says why the post doesn't match."""
NO_HOMEWORK: Final = "No homework was saved by this attempt."
"""What a press whose homework transaction didn't commit says of it where nothing else does."""
HOMEWORK_CHANGES_NOT_SAVED: Final = "The homework changes weren't saved."
"""What a Save says beside the answers it kept when the final check finds the homework changed
since its page: the answers stay stored, and a later Save meets the same check."""
PRESS_SAID: Final = {
    "answers_kept": "Your answers were kept by this attempt.",
    "some_kept": "Some of your answers were kept by this attempt.",
    "not_kept_for_saved": "This attempt did not keep answers for homework already saved.",
    "keep_warning": (
        "This attempt did not save the corrections you typed. Use Save review for later to "
        "keep them."
    ),
    "not_reviewed": (
        "Your corrections were not reviewed, so nothing was saved. Check them, then press "
        "Review changes."
    ),
    "keep_invalid": (
        "Some of these questions changed before you answered, so none of these answers was "
        "kept. Answer them again."
    ),
    "keep_failed": "Your answers couldn't be kept. Try again.",
    "too_long": (
        "A correction is longer than its field takes, so nothing was kept or saved. Shorten it "
        "and press again."
    ),
    "nothing_kept": "Nothing was kept or saved.",
    "sent_twice": (
        'The choice "Add this note to the assignment" was sent twice with different answers. '
        "Choose one, then press again."
    ),
    "unanswered_twice": (
        'The choice "Add this note to the assignment" sent twice with different answers needs '
        "your answer. Choose one, then press again."
    ),
    "field_twice": (
        "A box or a choice was sent twice with different answers. Each one is marked below with "
        "both answers. Enter or choose one, then press again."
    ),
    "field_unanswered": (
        "A box or a choice sent twice with different answers needs your answer. Each one is "
        "marked below with both answers. Enter or choose one, then press again."
    ),
    "too_large": f"This review is too large for Blossom to process. {NOTHING_BY_THIS_ATTEMPT}",
    "save_invalid": (
        "Some of these questions changed before you answered, so nothing was kept or saved. "
        "Answer them again."
    ),
    "save_failed": "Your answers couldn't be kept, and nothing was saved. Try again.",
    "later": "Your corrections are saved for later.",
    "later_behind": "Your corrections weren't saved for later by this attempt.",
    "later_invalid": (
        "Some of these questions changed before you answered, so nothing was kept or saved for "
        "later. Answer them again."
    ),
    "later_failed": "Your answers couldn't be kept, and nothing was saved for later. Try again.",
    "kept_before_ended": "Your answers were kept before this review ended.",
    "some_kept_before_ended": "Some of your answers were kept before this review ended.",
    "none_chosen": "Choose at least one assignment to save.",
    "choices_not_saved": (
        "Nothing was saved, and these choices are not kept yet. Use Save review for later to "
        "keep them."
    ),
    "split_waits": (
        "Your answer about different assigned days isn't kept yet. To see its cards, use Keep "
        "these answers or Save review for later under Other choices."
    ),
    "split_waits_nothing_saved": (
        "Nothing was saved, and these choices are not kept yet. To keep them and see the cards "
        "for your answer about different assigned days, use Keep these answers or Save review "
        "for later under Other choices."
    ),
    "held_back": (
        "Needs your attention: an assignment wasn't saved. Press Review changes to see what "
        "saving it will do."
    ),
    "held_open": (
        "Needs your attention: an assignment you ticked wasn't saved because a question on it "
        "needs an answer."
    ),
    "answer_first": "Answer first: {questions}.",
    "already": "Already saved from this review.",
    "changed": (
        "Saving an assignment you ticked would now do this instead. Check it and save again."
    ),
    "changed_beside_answers": f"{HOMEWORK_CHANGES_NOT_SAVED} Check the effects below, then save.",
    "busy": (
        "The homework wasn't saved because the household file was busy; nothing of it was "
        "written. Save again."
    ),
    "failed": "The homework wasn't saved; nothing of it was written. Save again.",
    "unsaved_typing": "Not saved yet: your changes on the rows below.",
    "incomplete": (
        "Homework saved. Blossom couldn't confirm this review is closed; its next hourly "
        "cleanup closes it if it's still open."
    ),
}
"""What the review's presses say, all in one place."""
NOT_SAVED_BECAUSE: Final = {
    "gate": "a screenshot isn't read yet",
    "open": "an assignment you ticked still has a question to answer",
    "names": (
        "the assignments you ticked come from screenshots with different names; save each "
        "name's assignments on their own"
    ),
}
"""Why a press's homework wasn't saved, for "The homework wasn't saved: {reason}."."""
ANSWERS_COME_BACK: Final = frozenset({"changed", "busy", "failed", *NOT_SAVED_BECAUSE})
"""The homework-save outcomes that write nothing and give the press's answers back as posted."""
ABOUT_THE_SCREENSHOTS: Final = frozenset({"name", "split"})
"""The answer kinds that describe the screenshots and no one card: a Save's homework transaction
stores them, and only beside homework it adds or updates."""


def wasnt_saved(reason: str) -> str:
    """Why a press's homework transaction didn't commit, by ``NOT_SAVED_BECAUSE``."""
    return f"The homework wasn't saved: {NOT_SAVED_BECAUSE[reason]}. Nothing of it was written."


UNSHOWN_SAID: Final = {
    PRESS_SAID["changed"]: (
        "Saving an assignment you ticked would now do something different. Check it in the "
        "review and save again.",
    ),
    PRESS_SAID["unsaved_typing"]: (),
    PRESS_SAID["changed_beside_answers"]: (
        f"{HOMEWORK_CHANGES_NOT_SAVED} Check the effects in the review, then save.",
    ),
}
"""The press lines that point at the review's rows, as a page that can't show them says them."""
ANOTHER_ATTEMPT: Final = "Blossom couldn't tell whether another attempt saved any homework."
"""What a Save press that wrote no homework says after that, where it can't ask whether another
attempt saved any."""
NO_HOMEWORK_BY_THIS_ATTEMPT: Final = (NO_HOMEWORK, ANOTHER_ATTEMPT)
"""The homework lines of a Save press whose own values show it wrote no homework and whose look
for a receipt was refused."""
TYPING_STILL_HERE: Final = "Your typing is still here."
"""What a refused Save for later adds to its line only where the page gives its typing back."""
THIS_ATTEMPT_SAVED: Final = "This attempt saved the review for later."
"""Save for later's confirmation where the read after its commit shows a newer revision."""


def typing_here(said: Iterable[str], typed: Sequence[tuple[str, str]]) -> list[str]:
    """A press's lines with ``TYPING_STILL_HERE`` after a refused Save for later's line only when
    the page gives back a copy of its typing (``typed``)."""
    behind = PRESS_SAID["later_behind"]
    return [f"{line} {TYPING_STILL_HERE}" if line == behind and typed else line for line in said]


def without(line: str, ending: str) -> str:
    """A line with its closing instruction left out."""
    return line.removesuffix(" " + ending)


ENDED_SAID: Final = {
    PRESS_SAID["answers_kept"]: PRESS_SAID["kept_before_ended"],
    PRESS_SAID["some_kept"]: PRESS_SAID["some_kept_before_ended"],
    PRESS_SAID["keep_warning"]: "",
    PRESS_SAID["changed_beside_answers"]: HOMEWORK_CHANGES_NOT_SAVED,
    PRESS_SAID["none_chosen"]: "",
    PRESS_SAID["choices_not_saved"]: "",
    PRESS_SAID["held_back"]: "",
    PRESS_SAID["busy"]: without(PRESS_SAID["busy"], "Save again."),
    PRESS_SAID["failed"]: without(PRESS_SAID["failed"], "Save again."),
    PRESS_SAID["keep_failed"]: without(PRESS_SAID["keep_failed"], "Try again."),
    PRESS_SAID["save_failed"]: without(PRESS_SAID["save_failed"], "Try again."),
    PRESS_SAID["later_failed"]: without(PRESS_SAID["later_failed"], "Try again."),
    PRESS_SAID["too_long"]: without(PRESS_SAID["too_long"], "Shorten it and press again."),
    OUT_OF_DATE: without(OUT_OF_DATE, "Check the review before trying again."),
    PAGE_SAID["both"]: "",
    PAGE_SAID["conflict"]: "",
    wasnt_saved("names"): (
        "The homework wasn't saved because the assignments you ticked come from screenshots "
        "with different names."
    ),
    PRESS_SAID["unsaved_typing"]: "",
}
"""A press's lines as the page for a review that expired, or that the sweep took away, says
them: each line saying this attempt kept answers, as kept before the review ended; each line
holding an instruction that needs that review, as listed here without it; each line that is only
such an instruction, or points at the review's rows, left out (empty), and Keep's corrections
warning with them. The changed line is said by ``ended_lines``, as are a save's counts; any
other line not listed is said as the review says it."""


COUNTS_WITH_SOME_LEFT: Final = re.compile(
    r"(?P<counts>Saved: \d+ new assignments?\.(?: \d+ updated\.)?(?: \d+ already up to date\.)?"
    r"|Nothing new to save: \d+ assignments? already up to date\.)"
    r" (?P<left>\d+) still needs? checking\."
)
"""A save's counts with something left to check, whole, as ``counted`` says them."""


def counts_then(line: str) -> str:
    """A save's counts as an ended review's page says them: the new, the updated and the already
    up to date as they are, and what was left as it stood at the save, "At the time of this save,
    1 item still needed checking.", since nothing of an ended review is left to check. Items,
    not assignments: what was left counts questions still open and cards that couldn't be read
    too. Any other line, and counts that left nothing, as it is."""
    found = COUNTS_WITH_SOME_LEFT.fullmatch(line)
    if found is None:
        return line
    left = int(found["left"])
    items = "item" if left == 1 else "items"
    return f"{found['counts']} At the time of this save, {left} {items} still needed checking."


def ended_lines(lines: Iterable[str], *, saved: bool) -> list[str]:
    """A press's lines as an ended review's page says them (``ENDED_SAID``). The changed line
    of a homework transaction that didn't commit says that no homework was saved; beside a
    save that committed (``saved``), it is left out. A save's counts say what was left as it
    stood at the save (``counts_then``)."""
    out = []
    for line in lines:
        if line == PRESS_SAID["changed"]:
            ended = "" if saved else NO_HOMEWORK
        else:
            ended = ENDED_SAID.get(line, counts_then(line))
        if ended:
            out.append(ended)
    return out


KEPT_SAID: Final = frozenset(
    {PRESS_SAID["answers_kept"], PRESS_SAID["some_kept"], PRESS_SAID["not_kept_for_saved"]}
)
"""The answers lines: what a press says its own write did with its answers."""
APART_SAID: Final = KEPT_SAID | {PRESS_SAID["keep_warning"]}
"""The lines a page with no form says apart from its problems, in the status style: the answers
lines, and the corrections warning that follows them."""


def answers_said(kept: AnswersKept) -> list[str]:
    """The answers lines of one press, from what its own committed write returned and nothing
    else: that its answers were kept when it stored some and dropped none; that some were, then
    that none were kept for homework already saved, when it stored some and dropped some; the
    second of those alone when it stored none and dropped some; nothing when it stored and
    dropped none. Answers already on file, and a withdrawal, add no line. ``ended_lines`` gives
    each line as a page says it once a read has said the review ended."""
    if kept.stored > 0 and kept.dropped > 0:
        return [PRESS_SAID["some_kept"], PRESS_SAID["not_kept_for_saved"]]
    if kept.stored > 0:
        return [PRESS_SAID["answers_kept"]]
    if kept.dropped > 0:
        return [PRESS_SAID["not_kept_for_saved"]]
    return []


def kept_then_ended(kept: AnswersKept) -> list[str]:
    """What a Save says of its own write where its review expired or was gone under its homework
    save, from what that committed write returned. When it stored some answers and dropped none:
    that its answers were kept before the review ended, then that no homework was saved by this
    attempt. When it stored some and dropped some: that some were kept before the review ended,
    that none were kept for homework already saved, and that no homework was saved by this
    attempt. When it stored none and dropped some: only that none were kept for homework already
    saved. When it stored and dropped none: nothing. No line here says that the review ended
    before homework was saved, or what another attempt saved."""
    if kept.stored > 0 and kept.dropped > 0:
        return [
            PRESS_SAID["some_kept_before_ended"],
            PRESS_SAID["not_kept_for_saved"],
            NO_HOMEWORK,
        ]
    if kept.stored > 0:
        return [PRESS_SAID["kept_before_ended"], NO_HOMEWORK]
    if kept.dropped > 0:
        return [PRESS_SAID["not_kept_for_saved"]]
    return []


def kept_lines(said: Iterable[str]) -> list[str]:
    """Of a press's lines, its answers lines."""
    return [line for line in said if line in KEPT_SAID]


def apart_lines(said: Iterable[str]) -> list[str]:
    """Of a press's lines, those a page with no form says apart from its problems."""
    return [line for line in said if line in APART_SAID]


CORRECTIONS_NOT_SAVED: Final = without(
    PRESS_SAID["keep_warning"], "Use Save review for later to keep them."
)
"""Keep's corrections warning with no instruction to use Save review for later."""


def apart_past_a_finished_read(said: Iterable[str]) -> list[str]:
    """Of a Keep's lines, those its page with no form says apart from its problems, as that page
    says them where a read the press has made says the review finished and not closed: each
    answers line as an ended review's page says it (``ENDED_SAID``), and the corrections warning
    with no instruction to use Save review for later (``CORRECTIONS_NOT_SAVED``), which a
    finished review refuses."""
    warning = PRESS_SAID["keep_warning"]
    return [
        CORRECTIONS_NOT_SAVED if line == warning else ENDED_SAID.get(line, line)
        for line in apart_lines(said)
    ]


def after_answers(lines: Sequence[str], beside: Sequence[str]) -> list[str]:
    """``lines`` with ``beside`` after the answers lines they open with."""
    at = next((place for place, line in enumerate(lines) if line not in KEPT_SAID), len(lines))
    return [*lines[:at], *beside, *lines[at:]]


UNCERTAIN_SAID: Final = frozenset({UNKNOWN_SAID["keep"], UNKNOWN_SAID["later"]})
"""The lines that say a press can't tell whether its own write to its answers was made."""


def uncertain_lines(said: Iterable[str]) -> list[str]:
    """Of a press's lines, those that say it can't tell whether its own write was made."""
    return [line for line in said if line in UNCERTAIN_SAID]


OWN_FAILURE_SAID: Final = UNCERTAIN_SAID | {
    PRESS_SAID["keep_failed"],
    PRESS_SAID["later_failed"],
    PRESS_SAID["save_failed"],
}
"""The lines that say a press's own write to its answers failed, or that it can't be told, as
Save's can't-tell line is Keep's. An earlier save's result doesn't settle any of them, so they go
on beside it, Save's busy sentence as ``beneath_a_result`` gives it."""
BENEATH_A_RESULT_SAID: Final = {
    PRESS_SAID["save_failed"]: PRESS_SAID["keep_failed"],
    without(PRESS_SAID["save_failed"], "Try again."): without(
        PRESS_SAID["keep_failed"], "Try again."
    ),
}
"""Save's busy sentence, with and without its instruction, as it is said beneath an earlier
save's result: that the answers couldn't be kept, since "nothing was saved" would deny that
result."""


def beneath_a_result(lines: Iterable[str]) -> list[str]:
    """A press's lines as they are said beneath an earlier save's result
    (``BENEATH_A_RESULT_SAID``)."""
    return [BENEATH_A_RESULT_SAID.get(line, line) for line in lines]


def with_no_retry(lines: Iterable[str]) -> list[str]:
    """A press's lines beside a homework outcome it can't settle: a busy write's sentence says
    only that the answers couldn't be kept, with no instruction to press again."""
    return [without(line, "Try again.") for line in beneath_a_result(lines)]


def own_failure_lines(said: Iterable[str]) -> list[str]:
    """Of a press's lines, those that say its own write failed, or can't be told."""
    return [line for line in said if line in OWN_FAILURE_SAID]


def behind_lines(said: Iterable[str]) -> list[str]:
    """Of a press's lines, Save for later's refusal as behind, which an earlier save's result
    doesn't settle either, so it goes on beneath that result."""
    return [line for line in said if line == PRESS_SAID["later_behind"]]


CONFIRMED_SAID: Final = frozenset(
    {
        PRESS_SAID["answers_kept"],
        PRESS_SAID["some_kept"],
        PRESS_SAID["kept_before_ended"],
        PRESS_SAID["some_kept_before_ended"],
    }
)
"""The lines that say a press kept answers, by this attempt or before the review ended."""
KEPT_BEFORE_IT_ENDED: Final = frozenset(
    {PRESS_SAID["kept_before_ended"], PRESS_SAID["some_kept_before_ended"]}
)
"""The lines that say a press kept answers before the review ended. A page says one only after a
read of its press has said the review ended."""
ANSWERS_SAID: Final = KEPT_SAID | CONFIRMED_SAID
"""The answers lines as any page says them: by this attempt, or before the review ended."""


def unconfirmed_lines(said: Iterable[str]) -> list[str]:
    """Of a press's lines, those that don't say it kept answers."""
    return [line for line in said if line not in CONFIRMED_SAID]


def saved_for_later_at(state: ApplicationState) -> str:
    """When a review was saved for later, as its page says it."""
    return f"Saved for later at {until_text(state, state.real_clock.now())}."


def counted(added: int, updated: int, already: int, left: int) -> str:
    """A save's counts: "Saved: 2 new assignments. 1 updated. 1 already up to date. 1 still
    needs checking.", each but the new ones said only when not zero; or, when nothing was new,
    "Nothing new to save: 2 assignments already up to date." with what still needs checking."""
    still = f" {left} still need{'s' if left == 1 else ''} checking." if left else ""
    if not added and not updated:
        return (
            f"Nothing new to save: {already} assignment{'' if already == 1 else 's'} already up "
            f"to date.{still}"
        )
    parts = [f"Saved: {added} new assignment{'' if added == 1 else 's'}."]
    if updated:
        parts.append(f"{updated} updated.")
    if already:
        parts.append(f"{already} already up to date.")
    return " ".join(parts) + still


def refused_said(post: ReviewPost) -> list[str]:
    """What a press refused whole for a box it can't take says: a correction too long, and each
    box that can't be left as posted, in the box's own words, a joined note's tick posted twice
    with different values, a box or answer posted twice with different values or marked so and
    left unanswered, and that nothing was kept or saved, once."""
    boxes = [NEEDED[field] for _, field in post.needed if field in NEEDED]
    boxes += [PAGE_SAID["both"]] if post.both else []
    boxes += [PAGE_SAID["conflict"]] if post.conflicted else []
    boxes += [PRESS_SAID["sent_twice"]] if post.sent_twice else []
    boxes += [PRESS_SAID["unanswered_twice"]] if post.unanswered else []
    boxes += [PRESS_SAID["field_twice"]] if post.twice else []
    boxes += [PRESS_SAID["field_unanswered"]] if post.left_open else []
    if post.too_long:
        return [PRESS_SAID["too_long"], *dict.fromkeys(boxes)]
    return [*dict.fromkeys(boxes), PRESS_SAID["nothing_kept"]]


def held_said(rows: RowsShown, post: ReviewPost, *, saved: bool) -> list[str]:
    """What a Save says of the groups it held back, by what each is waiting on on the page it
    returns. A group with a question left to answer, and no destination chosen on this press: that
    a question needs an answer, naming each one, beside the homework this press saved (``saved``)
    or as the reason none was saved. Any other held group: that Review changes shows what saving it
    will do."""
    held = [unit for unit in rows.units if post.held_back & set(unit.unit.split("+"))]
    asking = [
        unit
        for unit in held
        if unit.still_open and not post.chosen_here & set(unit.unit.split("+"))
    ]
    lines: list[str] = []
    if asking:
        questions = ", ".join(dict.fromkeys(name for unit in asking for name in unit.still_open))
        lines.append(PRESS_SAID["held_open"] if saved else wasnt_saved("open"))
        lines.append(PRESS_SAID["answer_first"].format(questions=questions))
    if len(asking) < len(held) or not held:
        lines.append(PRESS_SAID["held_back"])
    return lines


def not_saved(outcome: NotSaved, own_write: AnswersKept) -> list[str]:
    """What a press says when the homework transaction didn't commit: its answers lines
    (``answers_said``), and why nothing of the homework was written. A save refused as changed
    always says that the homework changes weren't saved and to check the effects, and gives no
    cause for the change. One refused because the review moved on since its page,
    by its revision or by its version, says that no homework was saved by this attempt and that
    the page is out of date, and gives no cause either; so does a reason not listed in
    ``NOT_SAVED_BECAUSE``."""
    kept = answers_said(own_write)
    if outcome.reason == "changed":
        if own_write.stored > 0:
            return [*kept, PRESS_SAID["changed_beside_answers"]]
        return [*kept, HOMEWORK_CHANGES_NOT_SAVED, PRESS_SAID["changed"]]
    if outcome.reason in ("busy", "failed"):
        return [*kept, PRESS_SAID[outcome.reason]]
    if outcome.reason not in NOT_SAVED_BECAUSE:
        return [*kept, NO_HOMEWORK, OUT_OF_DATE]
    return [*kept, wasnt_saved(outcome.reason)]


REVIEW_FORM_FIELDS: Final = 200_000
"""The most fields a post to a review's route is read with, whichever it is: one of its three
presses, an offer or a found row."""
REVIEW_FORM_BYTES: Final = 139_952_128
"""The most bytes as sent a post to a review's route is read to, all its fields together: room for
600 cards of 180 KiB, each with every box at its limit in characters of four bytes, its new
instruction as the same words, and the longest name its tick can carry, and 28 MiB more."""
REVIEW_FIELD_BYTES: Final = REVIEW_FORM_BYTES
"""The most bytes as sent one field of a post to a review's route is read with: as much as the whole
post."""


async def kept_words(state: ApplicationState, wanted: frozenset[str]) -> dict[str, dict[str, str]]:
    """The words of these assignments' kept instructions, by assignment and by the value a page
    offers each row with, from one read. A kept row's words never change, so its row names what its
    page showed."""
    if not wanted:
        return {}
    record = state.project_state
    found = await asyncio.to_thread(record.settled, record.school_instruction_readings, wanted)
    return {
        name: {kept_ref(item.sequence): item.text for item in one.kept}
        for name, one in found.readable.items()
    }


async def review_post(
    request: Request, state: ApplicationState, found: DraftSnapshot
) -> tuple[ReviewPost | None, str]:
    """The post read against what its page signed, or None when it doesn't match or the review
    has no homework to read it against, with the press it named. Kept rows are read only for a post
    its page signed. Anything in it nested too deep to read doesn't match either. A signed list past
    its ceiling is left for the caller to answer (``ListTooLarge``). A post that matches leaves the
    cards its boxes are drawn for on the request alone, to label its copies (``copy_label``)."""
    form = await form_of(request)
    items = [(name, value) for name, value in form.multi_items() if isinstance(value, str)]
    named = next((value for name, value in items if name == "press"), "keep")
    saved = await saved_corrections(state, found)
    review = await state_of_review(state, found, saved)
    if review is None:
        return None, named
    places = {image.sha256: index + 1 for index, image in enumerate(found.images)}
    load_ticks = selected_on_load(review, picked(found.progress))
    try:
        if any(name == "offer" for name, _ in items):
            post = with_selection_shown(read_change(items, found.page_key, review, saved), found)
            request.state.typed_cards = cards_named(review, places)
            return dataclasses.replace(post, held=review.held, load_ticks=load_ticks), "keep"
        kept = await kept_words(state, kept_assignments(items, found.page_key))
        post = read_review(items, found.page_key, review, kept)
        request.state.typed_cards = cards_named(review, places)
        post = typing_asks_again(review, post)
        return dataclasses.replace(post, held=review.held, load_ticks=load_ticks), named
    except ListTooLarge:
        raise
    except (PostRefused, RecursionError):
        return None, named


def open_row_keys(found: DraftSnapshot) -> list[str]:
    """The review's open rows, as a page counts them in the selection it signs."""
    return [row.key for row in found.rows if row.state == "open"]


def with_selection_shown(post: ReviewPost, found: DraftSnapshot) -> ReviewPost:
    """An offer's or a found row's post with what it gives saved progress: the selection its page
    showed and the rows of the cards it chose, compared against the revision that page showed. A
    form that carried no selection, or one counted over other rows, gives it nothing."""
    read = (
        None
        if post.selection_shown is None
        else selection_read(post.selection_shown, open_row_keys(found))
    )
    if read is None:
        return post
    revision, shown = read
    return dataclasses.replace(
        post, progress_revision=revision, selection=shown | rows_of_units(post.chosen_here)
    )


def typing_asks_again(review: ReviewState, post: ReviewPost) -> ReviewPost:
    """The post with each destination its own typing made stale taken back before anything holds
    or stores it: a saved assignment chosen on a card whose words differ from those its page
    signed as shown (``ReviewPost.unshown``), where that correction leaves the assignment offered
    only through the card as read. A signature travels through every page that signs its post's
    digests (``ReviewPost.shown_signed``), so words a review showed count as reviewed until they
    change, and words changed and changed back take nothing back. The page asks again with nothing
    chosen, and the same choice made on the page that shows the typing stands. Correct what was
    read gives every choice back as posted, and so does a press refused for a box it can't take, a
    conflict or a field sent twice (``ReviewPost.carries_its_page``), the next accepted press
    deciding. Worked out from the read
    the post was checked against, with no further read of the record."""
    if post.press.startswith(CORRECT_PRESS) or post.carries_its_page or not post.unshown:
        return post
    typed = {key for key, _ in post.unshown}
    pieces = {piece.name: piece for piece in review.pieces_with(post.corrections)}
    retaken = frozenset(
        answer.subject
        for answer in post.answers
        if answer.kind == "destination"
        and (piece := pieces.get(answer.subject)) is not None
        and typed & {piece.key, dates_key(piece.name)}
        and not offered_as_it_stands(piece, value_of(answer.value), review.rows, review.picks)
    )
    if not retaken:
        return post
    return dataclasses.replace(
        post,
        answers=tuple(
            answer
            for answer in post.answers
            if answer.kind != "destination" or answer.subject not in retaken
        ),
        choices={**post.choices, **{answer_field("destination", name): () for name in retaken}},
        retaken=retaken,
        chosen_here=post.chosen_here - retaken,
    )


async def save_pressed(request: Request) -> bool:
    """Whether this post names Save selected homework, read before it can be checked against its
    page, as a review that has closed can't check it."""
    form = await form_of(request)
    return next((value for name, value in form.multi_items() if name == "press"), "") == "save"


@router.post("/{draft_id}/review")
async def review_press(request: Request, draft_id: str, state: State) -> Response:
    """The review form's three presses, and an offer or a found row posted whole: Keep these
    answers, Save selected homework and Save review for later. A press with a correction
    longer than its field takes keeps and saves nothing, and answers 422 with the review and
    its typing, that correction whole in its box. Whichever post this is, its form is read first,
    within this route's limits, and every later read of it gets that one, so a body past them
    touches no store. A press whose signed list is past its ceiling keeps nothing and says the
    review is too large, and one whose post doesn't match its page says so; where the review can't
    be shown or has ended, or, on a Save press, a receipt answers or can't be read, that reason is
    said beside them, beneath a receipt's result, a mismatch's as nothing kept or saved by this
    attempt, since nothing says why. Keep and Save for later don't look for a receipt while the
    review can be read, so that answer says nothing of an earlier save. A review its first read
    finds closed, expired or gone answers with an earlier save's outcome first, then the typing to
    copy."""
    if not opaque(draft_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    request = WithinBytes(request, REVIEW_FORM_BYTES)
    await form_of(request, most_fields=REVIEW_FORM_FIELDS, most_field_bytes=REVIEW_FIELD_BYTES)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        found = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        noted(error)
        return await replayed(request, state, draft_id) or await unshown_unread(
            request, state, draft_id, error
        )
    except STORE_REFUSALS as error:
        return await replayed(request, state, draft_id) or await refused_keeping_typing(
            request, state, error
        )
    if found.closed_at is not None:
        return await closed_on_press(request, state, draft_id, 200)
    too_large = False
    try:
        post, named = await review_post(request, state, found)
    except ListTooLarge:
        post, named, too_large = None, "save" if await save_pressed(request) else "keep", True
    except sqlite3.Error as error:
        noted(error)
        return await replayed(request, state, draft_id) or await unshown_unread(
            request, state, draft_id, error
        )
    if post is None:
        refusal = [PRESS_SAID["too_large"] if too_large else NOTHING_BY_THIS_ATTEMPT]
        # Either refusal says nothing was saved by this attempt, and no write follows it.
        another = [ANOTHER_ATTEMPT]
        recorded = (
            await replayed(request, state, draft_id, refusal, notes=refusal, homework=another)
            if named == "save"
            else None
        )
        if recorded is not None:
            return recorded
        ended = await ended_under_press(
            request, state, draft_id, 409, refusal=refusal, homework=another
        )
        if ended is not None:
            return ended
        if too_large:
            return to_review(state, draft_id, [PRESS_SAID["too_large"]])
        refused = {"save": "save_invalid", "later": "later_invalid"}.get(named, "keep_invalid")
        return to_review(state, draft_id, [PRESS_SAID[refused]])
    if post.refused:
        recorded = await replayed(request, state, draft_id) if post.press == "save" else None
        chosen = (
            post.chosen_here if post.press == "save" and post.version == found.version else None
        )
        # A Correct press refused whole, and any press but Save refused for a box, a conflict or a
        # field sent twice, show no review of their typing: their page's signatures travel on.
        carries = post.press.startswith(CORRECT_PRESS) or post.carries_its_page
        return recorded or await with_typing(
            request,
            state,
            draft_id,
            post if chosen is None else dataclasses.replace(post, held_back=chosen),
            refused_said(post),
            422,
            found=found,
            opened=frozenset(post.opened),
            signed_digests=post.digests if carries else None,
            held_answers=destinations_held(post),
        )
    if post.press == "review" or post.press.startswith(CORRECT_PRESS):
        return await nothing_written(request, state, found, post)
    if post.press == "later":
        return await later_press(request, state, found, post)
    if post.press == "save":
        return await save_press(request, state, found, post)
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
    except sqlite3.Error as error:
        noted(error)
        return await press_refused(
            request, state, draft_id, post, error, "keep_failed", found=found
        )
    try:
        kept = await asyncio.to_thread(
            state.import_drafts.keep_answers,
            student,
            draft_id,
            version=post.version,
            answers=list(post.answers),
            withdrawn=list(post.withdrawn),
            selection=selection_to_keep(post, found),
            progress_revision=post.progress_revision,
        )
    except ReviewChanged:
        return await with_typing(request, state, draft_id, post, [OUT_OF_DATE], 409, found=found)
    except (StoreBusy, WriterBusy):
        said = [PRESS_SAID["keep_failed"]]
        return await with_typing(request, state, draft_id, post, said, 503, found=found)
    except DraftClosed:
        return await closed_on_press(request, state, draft_id, 409, rows=False)
    except (DraftExpired, DraftNotFound) as error:
        return await refused_keeping_typing(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        unknown = [UNKNOWN_SAID["keep"]]
        return await press_refused(
            request, state, draft_id, post, error, "keep_failed", unknown, found=found
        )
    answered = kept.stored > 0
    warning = [PRESS_SAID["keep_warning"]]
    return await with_typing(
        request,
        state,
        draft_id,
        post,
        [*answers_said(kept), *(warning if answered else [])],
        200,
        answered=answered,
        beside_rows=[] if answered else warning,
        found=found,
        own_write=kept,
        opened=frozenset(post.opened),
    )


async def nothing_written(
    request: Request, state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> Response:
    """Correct what was read and Review changes, which write nothing and store no answer: Correct
    what was read comes back with that card's boxes open beside those already open, the typing,
    choices and ticks as posted, each group's digests and each correction shown as the post's page
    signed them, and the post's destinations held (``destinations_held``); Review changes comes
    back with every card closed
    and the corrected proposal signed, worked out with the post's answers held as keeping them
    would hold them (``as_kept``), so every question it raises is asked before Save and Save then
    writes it once."""
    review = post.press == "review"
    return await with_typing(
        request,
        state,
        found.draft_id,
        post,
        [],
        200,
        opened=frozenset() if review else frozenset(post.opened),
        signed_digests=None if review else post.digests,
        as_posted=True,
        held_answers=post if review else destinations_held(post),
    )


def selection_of(post: ReviewPost) -> Callable[[str | None], str | None]:
    """What a press that keeps the review gives saved progress: the groups it ticked, or the rows
    an offer or a found row carries, in place of the selection stored (``selection_text``)."""
    selected = selected_by(post)
    return lambda progress: selection_text(progress, selected)


def selected_by(post: ReviewPost) -> frozenset[str]:
    """The rows a press that keeps the review selects: those of the groups it ticked, or those an
    offer or a found row carries."""
    return rows_of_units(post.units) if post.selection is None else post.selection


def selection_settled(
    post: ReviewPost,
    found: DraftSnapshot,
    stored: Iterable[AnswerToKeep],
    closed: frozenset[str] = frozenset(),
) -> bool:
    """Whether a press selects what a plain load shows once its answers (``stored``) are stored and
    the rows in ``closed`` are saved: with no saved progress, a destination stored takes its row
    off the default rule. An answer that can regroup the rows is never settled here, and the
    selection is written."""
    regroups = any(kind in ("destination", "joined") for kind, _ in post.withdrawn)
    answers = list(stored)
    if post.load_ticks is None or regroups or any(a.kind == "split" for a in answers):
        return False
    given = rows_of_units(a.subject for a in answers if a.kind == "destination")
    shown = post.load_ticks if found.progress is not None else post.load_ticks - given
    return shown - closed == selected_by(post) - closed


def selection_to_keep(
    post: ReviewPost, found: DraftSnapshot
) -> Callable[[str | None], str | None] | None:
    """What Keep these answers, an offer or a found row gives saved progress: nothing where it
    selects what a plain load shows, so an ordinary press leaves every other open page current."""
    if (not post.ticked and not post.selection) or selection_settled(post, found, post.answers):
        return None
    return selection_of(post)


async def load_shows(
    state: ApplicationState, student: str, draft_id: str, selected: frozenset[str]
) -> bool:
    """Whether a plain load of the review as it stands ticks exactly the rows of ``selected`` left
    open, read after the press's own commit. A read refused here tells nothing: False."""
    try:
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
        review = await state_of_review(state, snapshot, await saved_corrections(state, snapshot))
    except (*STORE_REFUSALS, sqlite3.Error) as error:
        logger.warning("A selection couldn't be compared: %s", type(error).__name__)
        return False
    if review is None:
        return False
    shown = selected_on_load(review, picked(snapshot.progress))
    return shown == selected & frozenset(open_row_keys(snapshot))


def destinations_held(post: ReviewPost) -> ReviewPost:
    """The post with only its answers of where each card goes that its page chose by no default,
    for a page that stores nothing and gives every other choice back as posted in its own
    question: held as keeping them would hold them (``as_kept``), they raise on that page every
    question they raise on Review changes. A destination the page chose by default is worked out
    as that default again, or, where the post's corrections withdraw that default, as sent when
    it was sent unchanged (its ``defaults``: the page defaults sent unchanged), still a default;
    one changed from it is given back as posted, "Not saved yet." beside it, its effect shown by
    Review changes. A joined note's tick the post left blank is held as that answer, so the tick
    stands as posted."""
    return dataclasses.replace(
        post,
        answers=(
            *(
                answer
                for answer in post.answers
                if answer.kind == "destination" and answer.subject not in post.defaults
            ),
            *(
                AnswerToKeep(kind, subject, UNJOINED, "", ())
                for kind, subject in post.withdrawn
                if kind == "joined"
            ),
        ),
        withdrawn=(),
        defaults={
            answer.subject: answer.value
            for answer in post.answers
            if answer.kind == "destination" and post.defaults.get(answer.subject) == answer.value
        },
    )


async def unreviewed_cards(
    state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> frozenset[str]:
    """The cards of a Save at its page's version that carries corrections no review showed: those
    whose effect the signed digests tell (``unreviewed_effects``) and those whose carried words no
    review showed (``unreviewed_words``). Empty where the review has moved past the page, which
    its stale-record refusal answers."""
    if post.version != found.version:
        return frozenset()
    return (
        await unreviewed_words(state, found, post)
        | await unreviewed_shown(state, found, post)
        | await unreviewed_effects(state, found, post)
    )


async def unreviewed_shown(
    state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> frozenset[str]:
    """The cards of a Save carrying a correction its page didn't sign as shown on the card
    (``ReviewPost.unshown``) that the saved progress doesn't keep: on any group, whether or not
    its digests can be worked out or were signed, so a group with a question still open is
    checked as one with digests is."""
    if not post.unshown:
        return frozenset()
    kept = await saved_corrections(state, found)
    changed = {
        key
        for (key, field), value in post.unshown.items()
        if field not in kept.get(key, {}) or value != kept[key][field]
    }
    if not changed:
        return frozenset()
    review = await state_of_review(state, found, post.corrections)
    pieces = () if review is None else review.pieces
    names = frozenset(
        piece.name for piece in pieces if piece.key in changed or f"date:{piece.name}" in changed
    )
    # Refused whatever its cards: one no read places is named by its key.
    return names or frozenset(key.removeprefix("date:") for key in changed)


async def unreviewed_words(
    state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> frozenset[str]:
    """The cards of a Save whose date Blossom couldn't read, carried back beside its box as the
    box's correction, holds words its page didn't sign as shown (``ReviewPost.carried``) and the
    saved progress doesn't keep: a family answer the page carries hidden, whose effect leaves the
    signed digests as they were, so no review showed it."""
    if not post.carried:
        return frozenset()
    kept = await saved_corrections(state, found)
    return frozenset(
        key.removeprefix("date:")
        for (key, field), words in post.carried.items()
        if post.corrections.get(key, {}).get(field) == words.strip()
        and words.strip() != (kept.get(key, {}).get(field) or "").strip()
    )


async def unreviewed_effects(
    state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> frozenset[str]:
    """The cards of a Save at its page's version that carries corrections no review showed: each
    group's digests are worked out with the posted corrections against the stored answers, so a
    tick or a destination never counts as a correction, and compared with those its page signed;
    a page Review changes made signs them with every answer of its post held (``as_kept`` on a
    Review changes press), an answer that replaces a kept one included, so a group matching those
    is reviewed too. Every card of a group that matches neither. A group its page signed with no
    digests, its destination asked or the group held back, shows no effect to compare and is never
    written by this press: ``unreviewed_shown`` checks its corrections, and it is held. Empty where
    each group matches, where the post carries no correction, and where the review has moved past
    the page, which its stale-record refusal answers."""
    if not post.corrections or post.version != found.version:
        return frozenset()
    review = await state_of_review(state, found, post.corrections)
    if review is None:
        return frozenset()
    worked = unit_digests(review)
    signed = {unit: digests for unit, digests in post.digests.items() if digests}
    # A group the page signed whose cards the stored answers place nowhere (its destination
    # posted, not stored) has no digests here: it is compared with the post's answers held, and
    # only where the post's corrections change it from the corrections the draft keeps, so its
    # choices alone never count as a correction.
    placed = {name for unit in worked for name in unit.split("+")}
    unplaced = {
        unit for unit in signed if unit not in worked and placed.isdisjoint(unit.split("+"))
    }
    differ = {
        unit for unit, digests in worked.items() if signed.get(unit, digests) != digests
    } | unplaced
    if not differ:
        return frozenset()
    as_on_review = dataclasses.replace(post, press="review", answers=destinations_as_shown(post))
    held = await state_of_review(state, found, post.corrections, as_on_review)
    as_reviewed = {} if held is None else unit_digests(held)
    if unplaced:
        kept = await saved_corrections(state, found)
        plain = await state_of_review(state, found, kept, as_on_review)
        as_kept_here = {} if plain is None else unit_digests(plain)
        differ -= {unit for unit in unplaced if as_kept_here.get(unit) == as_reviewed.get(unit)}
    return frozenset(
        name for unit in differ if signed[unit] != as_reviewed.get(unit) for name in unit.split("+")
    )


def destinations_as_shown(post: ReviewPost) -> tuple[AnswerToKeep, ...]:
    """The post's answers with each destination changed on this press put back to the one its page
    showed, or left out where its page showed none: a typed edit is checked at the destination
    its review showed, so a changed destination beside a reviewed correction is held, not
    refused."""
    answers = []
    for answer in post.answers:
        if answer.kind != "destination" or answer.subject not in post.chosen_here:
            answers.append(answer)
        elif answer.subject in post.shown_chosen:
            answers.append(answer._replace(value=post.shown_chosen[answer.subject]))
    return tuple(answers)


async def unshown_unread(
    request: Request, state: ApplicationState, draft_id: str, error: Exception
) -> Response:
    """A press whose review the household's file refuses to read before anything was written:
    for a busy store, that nothing changed; for any other refusal, only that the review can't
    be shown; with the typing given back as sent on every press."""
    said = [AFTER_SAID["busy"]] if busy_refusal(error) else []
    return await unshown_with_typing(request, state, draft_id, said, 503, as_sent=True)


async def press_refused(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    post: ReviewPost,
    error: sqlite3.Error,
    failed: str,
    unknown: Sequence[str] = (),
    *,
    found: DraftSnapshot | None = None,
    homework: Sequence[str] = (UNKNOWN_SAID["homework"],),
) -> Response:
    """Keep, Save or Save for later refused by the household's file, with the typing kept: a
    busy file as the press's own failure line (``failed``), since it wrote nothing; any other
    refusal, at a read before the press's write, as the review that can't be shown, and at the
    write, as the press's own lines for a write whose outcome isn't known (``unknown``). An offer's
    or a found row's form signs the revision of the read that filled it (``found``). A Save a
    busy file refused passes its homework lines on (``homework``, as ``with_typing`` takes
    them)."""
    said = [PRESS_SAID[failed]]
    if busy_file(error):
        return await with_typing(
            request, state, draft_id, post, said, 503, found=found, homework=homework
        )
    if unknown:
        return await with_typing(request, state, draft_id, post, unknown, 503, found=found)
    return await unshown_with_typing(request, state, draft_id, [], 503)


async def kept_past_empty_rows(
    state: ApplicationState, draft_id: str, notes: Sequence[str], counted: bool, ticked: bool
) -> Sequence[str]:
    """What a press's own write kept (``notes``) on a receipt's page whose rows came back empty,
    which can't tell an ending from nothing open. A press that ticked a group (``ticked``) reads
    nothing more, and its lines are said as they reach this page: as an ended review's page says
    them where an earlier read of the request found the review expired, removed or closed, and
    by this attempt otherwise, also where a first page read was refused as finished and a
    receipt was then found. For a press with nothing ticked, one more read says how those lines
    are said, as the page's own read does: as an ended review's page says them where it says the
    review ended; past a close with no counts (``counted``), only that this attempt did not keep
    answers for homework already saved, which claims nothing of what is on file; and as they
    stand where it reads open or is refused. A press with no line saying it kept answers reads
    nothing more either, and its lines are said as they stand: that its write failed or can't be
    told, or that it did not keep answers for homework already saved."""
    if ticked or not set(notes) & CONFIRMED_SAID:
        return notes
    now = await review_state_now(state, draft_id)
    if now == "busy":
        return notes
    if isinstance(now, str):
        return ended_lines(notes, saved=True)
    if now.closed_at is not None and not counted:
        return [line for line in notes if line == PRESS_SAID["not_kept_for_saved"]]
    if now.closed_at is None and not now.finished:
        return notes
    return ended_lines(notes, saved=True)


async def with_typing(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    post: ReviewPost,
    said: Sequence[str],
    status: int,
    done: Sequence[str] = (),
    read: DraftSnapshot | None = None,
    saved: Saved | None = None,
    answered: bool = False,
    unsaved: Collection[tuple[str, str]] = (),
    own: frozenset[str] | None = None,
    *,
    beside_rows: Sequence[str] = (),
    notes: Sequence[str] = (),
    counted: bool = True,
    found: DraftSnapshot | None = None,
    own_write: AnswersKept | None = None,
    homework: Sequence[str] = (UNKNOWN_SAID["homework"],),
    opened: frozenset[str] = frozenset(),
    signed_digests: Mapping[str, tuple[str, ...]] | None = None,
    as_posted: bool = False,
    held_answers: ReviewPost | None = None,
    left_answers: bool = False,
    stored_here: frozenset[str] = frozenset(),
) -> Response:
    """A press that saved nothing, or a save that left the review open, answers with the review as
    it stands now, the corrections it typed and the groups it ticked in the form, and an answer just
    kept stays matched to the corrections it was given with. Text no box here holds comes back,
    where this says it does, only as a copy on this page. A press that posts no ticks, an offer or a
    found row, is ticked as a plain load would tick the page. A review that expired meanwhile
    answers with what the press did and its typing to copy. A caller that has just read the review
    passes that read (``read``), so a later one can't answer differently. The rows come from the
    review's own tables, read afresh: a save that committed (``done``) and finds no rows there, as
    when another tab has just closed the review, answers with its result, the typing and the way
    back, and says nothing of rows it doesn't show. Where it does show rows, each typed field ends
    in a box holding it, in the save's evidence (``saved``) or in a box to copy, and two kinds of
    line are said here, beside them: the answers lines of that save's own write (``own_write``, by
    ``answers_said``), which each page after that save says beneath its result, apart from the
    page of a save that itself closed the review and a page that fails after the commit, which
    say no answers line, and that changes on the rows below aren't saved yet, when a box for one
    (``unsaved``) is among those shown. A press with no save committed carries its answers lines
    in ``said``, and a line of its own that is said only where rows were read (``beside_rows``)
    follows them. A Save press
    with no save committed that can't ask for its receipt says its homework lines (``homework``,
    as ``unknown_outcome`` says them). Each box holds the text its post sent where that equals
    its value once canonical
    (``boxes_as_sent``). Any press with no save committed gives back each box as sent that no box
    holds exactly (``typing_by_press``), labeled by the card the form draws it on (``cards_drawn``),
    under ``UNSHOWN_TYPED``, ``TYPED_SAID`` on a press refused with its choices; Keep or Save for
    later adds what "No assigned date was given" set aside or was refused beside, the words and
    anything sent in the box beside them, under ``UNSHOWN_TYPED`` (``tick_set_aside``), and signs
    the revision its page read. An earlier save's page with rows gives back what no box holds under
    ``UNSHOWN_TYPED`` too, labeled the same way.
    Any press with no save committed, if its review has ended before its rows, says that instead of
    its own line, and a Save press that meets a transaction left open at its rows read answers as
    ``unknown_outcome`` does. It
    keeps a line saying a press's own write to its answers failed, or that it can't be told
    (``own_failure_lines``). With
    no save committed, ``answered`` says this press's own write stored answers, for the pages
    ``ended_under_press`` answers with where the rows come back empty, and ``beside_rows`` are lines
    said after the press's answers lines and ahead of its others, only where rows are drawn. A
    Save press asks for its receipt before any page that ends it, as ``snapshot_keeping_typing``
    does. A press with an instruction conflict, or naming a kept row that isn't on the record
    (``unresolved``), comes back with its choices as posted while the review is at the version its
    page showed. At another, the review has changed, and carrying a choice onto a page of the new
    version would get it past the version check, so it says the page is out of date and carries
    none. Whatever can't carry is said, beside a confirmed save's counts too, with no word there of
    the page being out of date; only what that save took goes unsaid, as those are always gone: its
    own rows' questions and ticks (``own``, their pieces), and a name question, apart from an answer
    she cleared that the record still holds (``choices_said_lost``). A row that isn't on the record
    is always said. A Save press that finds a confirmed receipt (``own``, otherwise None) writes
    nothing more, and comes back the same way with no conflict: at the page's version its choices as
    posted, and at another, or past the page's revision, that they can't be shown if they aren't
    what the record holds or aren't offered, with one exception (``choices_said_lost``). Any other
    press refused at its page's version and revision, by a too long or cleared box (422) or a busy
    write (503), comes back with its choices as posted too; at another version, it carries none and
    says they can't be shown where one it posted isn't what the page shows (``choices_unlike``). An
    offer's or a found row's destination the page doesn't select is named, out of date
    (``RowsShown.unselected``). What a receipt's press kept itself (``notes``) goes in the page's
    status block, ahead of "Not saved yet", where rows are
    drawn, and beneath the save's result, outside the problem alert, where they aren't: as it
    stands, or if the rows came back empty and the press ticked no group, as one more read has
    it (``kept_past_empty_rows``, ``counted``). Rows that come back empty with the review open
    draw no form, and give every box
    back to copy; behind, beside words a tick set aside, they say its choices can't be shown where
    the saved progress of the review's own read, or the answers the press's latest read of the
    review holds, differ from them or can't be compared with them (``words_beside_unlike``). With
    no read after its write, those answers are this press's own write's result (``own_write``,
    given only after it returned) as ``written_answers`` gives them, else its read before the press.
    On that page a Keep whose read of the review, or whose read after its rows came back empty,
    says the review finished and not closed says its answers lines as an ended review's page says
    them, and its corrections warning with no instruction to use Save review for later
    (``apart_past_a_finished_read``). Choosing those lines reads nothing, and every other press
    says its lines as they stand.
    The form signs the revision its content was typed against: the post's; for an offer or a found
    row, that of the read that filled its boxes (``found``); after a save that committed, the one
    its own transaction raised the review to (``saved``). A press that writes nothing by design,
    Correct what was read, Review changes, or a Save refused for corrections no review showed
    (``as_posted``), comes back with its choices as posted at the page's version, the cards in
    ``opened`` open and ``signed_digests`` signed with the corrections its post's page signed as
    shown (``ReviewPost.shown_signed``, ``rows_of``). A page that stores nothing works
    its proposal out with the answers in ``held_answers`` held as keeping them would hold them,
    nothing stored: Review changes all its post's, and Correct what was read and each refusal that
    keeps the typing its post's destinations (``destinations_held``), so every question that
    proposal raises is asked there, and each destination it chose selects that card's group
    (``ReviewPost.chosen_here``). Where the review's stored revision is another, the page draws
    no form, says its choices can't be shown where it posted
    any (words a tick set aside aren't one), or where its post's "No assigned date was given" on
    a row left open isn't what the saved progress its rows were drawn from holds
    (``tick_unlike_progress``) or has no correction to compare with it, and answers no
    confirmation the answers they read hold as posted, beside saved progress absent or readable
    with no assigned date for it (``tick_beyond_progress``), and says it's out of date
    (``no_form_with_typing``), a Save with nothing saved as ``not_saved`` says it. Each page
    here that draws no form after this request's own save leaves out of its copies the class,
    title and instructions boxes that save's evidence holds (``text_boxes``). A Save that committed
    beside answers about groups it left open, none of them stored, and a Save whose homework save
    wrote nothing, give those back as posted (``left_answers``), unless another tab moved the
    review past this press's own write. An answer this press stored (``stored_here``, by field) is
    on the record, and is never counted among the choices a page can't show. A save that committed
    says a choice can't be shown only where it is more than that save took with the rows it wrote
    (``RowsShown.beyond_its_save``), and a Save's line about the groups it held back is said by
    what each waits on (``held_said``). A split answer that comes back unstored says where to keep
    it (``with_split_waiting``). A destination chosen on a press whose choices come back selects
    its group, and a Save holds that group for Review changes; a form that posts no ticks selects
    the group it chose for too."""
    saved_exactly = text_boxes(saved.stored) if saved else frozenset()
    stored = answers_said(own_write) if done and own_write is not None else []
    after = read or await snapshot_keeping_typing(
        request,
        state,
        draft_id,
        said,
        status,
        done,
        stored=stored,
        save_press=post.press == "save",
        saved_exactly=saved_exactly,
        homework=homework,
    )
    if isinstance(after, Response):
        return after
    if saved is None and own_write is not None and after.version != own_write.version:
        # Another tab moved the review past this press's own write: nothing carries past it.
        left_answers = False
    if post.held_back or left_answers:
        # A Save that held groups back gives them back as posted, whatever version its own write
        # moved the review to; they are signed with no digests, so only Review changes leads on.
        # Answers about a group left unticked come back the same way, its tick as posted.
        as_posted = True
        held_answers = held_answers or dataclasses.replace(post, press="review")
    if own_write is not None and own_write.progress_revision is not None:
        revision = own_write.progress_revision
    elif found is not None and not post.ticked:
        revision = found.progress_revision
    elif saved is not None:
        revision = saved.progress_revision
    else:
        revision = post.progress_revision
    behind = after.progress_revision != revision
    pressed = said
    lines = [*said, *notes]
    ticked = post.units if post.ticked else None
    took = (
        own if own is not None else frozenset(n for n, _ in saved.pairs) if saved else frozenset()
    )
    current = post.version == after.version or bool(post.held_back) or left_answers
    refused_with_choices = bool(post.conflicted or post.unresolved)
    with_choices = refused_with_choices or bool(post.both)
    carry = (
        refused_with_choices
        or own is not None
        or (current and not behind and (as_posted or status in (422, 503)))
    )
    stale = found is not None and not post.ticked and not current
    if refused_with_choices and not current:
        lines += [PAGE_SAID["not_carried"]] if done else [OUT_OF_DATE, PAGE_SAID["not_carried"]]
    answers_read: list[tuple[Mapping[tuple[str, str], str], Corrections] | None] = []
    answers_said_lost: list[Mapping[tuple[str, str], str]] = []
    try:
        rows = await rows_of(
            request,
            state,
            after,
            lines,
            typed=post.corrections,
            ticked=ticked,
            both=post.both,
            choices=(
                {name: sent for name, sent in post.choices.items() if name not in stored_here}
                if current and carry
                else None
            ),
            conflicted=post.conflicted if current else None,
            revision=revision,
            own=took,
            answers_read=answers_read,
            destinations={a.subject: a.value for a in post.answers if a.kind == "destination"}
            if stale
            else None,
            sent_twice=frozenset(post.sent_twice) if current else frozenset(),
            unresolved=post.open_choices if current else None,
            twice={**post.twice, **post.left_open} if current else None,
            opened=opened,
            signed_digests=signed_digests if current else None,
            shown_signed=post.shown_signed if current else None,
            posted=held_answers if current else None,
            unchosen=post.unchosen if current else frozenset(),
            retaken=post.retaken if current else frozenset(),
            chosen_here=(
                post.chosen_here
                if not post.ticked or (current and held_answers is not None)
                else frozenset()
            ),
            left_off=post.left_off if current else frozenset(),
            held_back=post.held_back - post.held_asking,
        )
        said_lost = await choices_said_lost(
            state, draft_id, post, rows, own, current, behind, answers_read=answers_said_lost
        )
    except sqlite3.Error as error:
        noted(error)
        press_kept = [*kept_lines(said), *own_failure_lines(said)]
        if isinstance(error, TransactionLeftOpen) and post.press == "save" and not done:
            return await unknown_outcome(request, state, press_kept, homework)
        looks = post.press == "save" and not done
        recorded = (
            await replayed(
                request, state, draft_id, press_kept, notes=press_kept, homework=homework
            )
            if looks
            else None
        )
        return recorded or await unshown(
            request,
            state,
            draft_id,
            said,
            status,
            done,
            notes=[*stored, *notes],
            saved_exactly=saved_exactly,
        )
    sent: list[tuple[str, str]] = []
    if rows is not None:
        lines[len(said) + len(notes) : len(said) + len(notes)] = rows.unselected
        with contextlib.suppress(FormUnreadable):
            form = await form_of(request)
            sent = [(name, value) for name, value in form.multi_items() if isinstance(value, str)]
            rows = boxes_as_sent(rows, sent)
    if rows is None and not done:
        read_again: list[DraftSnapshot] = []
        behind_said = behind_lines(said)
        ended = await ended_under_press(
            request,
            state,
            draft_id,
            200,
            kept_lines(said),
            answered=answered,
            corrections=behind_said if post.press == "later" else [PRESS_SAID["keep_warning"]],
            failed=own_failure_lines(said),
            beside_a_receipt=behind_said,
            read_again=read_again,
            homework=homework,
        )
        if ended is not None:
            return ended
        unshown_lines = lines[len(said) + len(notes) :]
        if answers_read:
            held, progress = answers_read[-1] or (None, None)
        elif own_write is not None:
            held, progress = written_answers(post, own_write), None
        else:
            held, progress = post.held, None
        newer = read_again[-1] if read_again else None
        if post.unresolved or (
            behind and (with_choices or words_beside_unlike(after, post, held, progress, newer))
        ):
            unshown_lines += [] if PAGE_SAID["not_carried"] in lines else [PAGE_SAID["not_carried"]]
        apart = notes
        finished = after.finished or (newer is not None and newer.finished)
        if post.press == "keep" and finished:
            apart = [*apart_past_a_finished_read(pressed), *notes]
            pressed = [line for line in pressed if line not in APART_SAID]
        return await no_form_with_typing(
            request,
            state,
            draft_id,
            [*pressed, *unshown_lines],
            status,
            notes=apart,
            behind=behind,
            finished=finished,
            saved_exactly=saved_exactly,
        )
    if rows is not None and carry:
        ticks_shown = {unit.unit for unit in rows.units if unit.ticked}
        if own is not None:
            ticks = [unit for unit in post.units if not own.issuperset(unit.split("+"))]
            lost = rows.beyond_its_save or not ticks_shown.issuperset(ticks)
            if said_lost or (current and lost):
                lines.append(PAGE_SAID["not_carried"])
        elif said_lost or (
            current
            and (
                (rows.beyond_its_save if saved is not None else rows.not_carried)
                or (refused_with_choices and not ticks_shown.issuperset(post.units))
            )
        ):
            lines.append(PAGE_SAID["not_carried"])
    if post.unresolved and PAGE_SAID["not_carried"] not in lines:
        lines.append(PAGE_SAID["not_carried"])
    uncarried = with_choices or PAGE_SAID["not_carried"] in lines
    if rows is not None:
        lines = after_answers(lines, beside_rows)
    shown = fields_shown(rows)
    if any((key, field) in unsaved for key, field, _ in shown):
        lines.insert(len(said) + len(notes), PRESS_SAID["unsaved_typing"])
    if behind and rows is not None:
        unlike = tick_unlike_progress(after, post.corrections, rows.progress)
        latest = answers_said_lost[-1] if answers_said_lost else rows.held
        unlike = unlike or tick_beyond_progress(after, post, rows.progress, latest)
        carried = [PAGE_SAID["not_carried"]] if uncarried or unlike else []
        told = [*done, *stored]
        return await no_form_with_typing(
            request,
            state,
            draft_id,
            [*after_answers(pressed, beside_rows), *carried],
            status,
            told,
            notes=notes,
            behind=True,
            saved_exactly=saved_exactly,
        )
    moved = not current and status in (409, 422, 503) and own is None
    reads = {name: value for name, value in sent if name.startswith("read:")}
    no_date_read = {
        name.removeprefix("read:") for name, value in reads.items() if value == read_digest("")
    }
    if (
        moved
        and not refused_with_choices
        and rows is not None
        and choices_unlike(post, rows, no_date_read)
    ):
        lines += [] if PAGE_SAID["not_carried"] in lines else [PAGE_SAID["not_carried"]]
    if rows is not None and PRESS_SAID["held_back"] in lines:
        at = lines.index(PRESS_SAID["held_back"])
        lines[at : at + 1] = held_said(rows, post, saved=bool(done))
        lines = list(dict.fromkeys(lines))
    if rows is not None and split_waiting(rows, post):
        lines = with_split_waiting(lines)
    drawn = {} if rows is None else cards_drawn(rows)
    if not done:
        typed = await typing_by_press(
            request,
            shown=shown,
            as_sent=True,
            every_press=True,
            drawn=drawn,
            undrawn_beside_words=True,
        )
        note = TYPED_SAID if refused_with_choices else UNSHOWN_TYPED
        if post.press != "save" and post.dropped:
            set_aside = tick_set_aside(request, post, shown, drawn)
            typed += [item for item in set_aside if item not in typed]
            note = UNSHOWN_TYPED
        return review_page(
            request, state, after, lines, status, rows=rows, typed=typed, typed_said=note
        )
    if rows is None:
        kept_line = await kept_past_empty_rows(state, draft_id, notes, counted, bool(post.units))
        return await saved_busy(
            request,
            state,
            draft_id,
            done,
            ended_lines(said, saved=True),
            notes=[*stored, *kept_line],
            saved_exactly=saved_exactly,
        )
    typed = await typing_of(
        request, saved.stored if saved else (), shown, as_sent=True, beside_words=True, drawn=drawn
    )
    told = [*done, *stored, *screenshot_answers_said(saved)]
    return review_page(
        request,
        state,
        after,
        lines,
        status,
        rows=rows,
        done=told,
        typed=typed,
        typed_said=UNSHOWN_TYPED,
    )


def screenshot_answers_said(saved: Saved | None) -> list[str]:
    """What a Save's page with a form says of the name and split answers its homework transaction
    stored: how many, that they are about the screenshots, and that they save no other
    assignment."""
    count = len(saved.screenshot_answers_kept) if saved is not None else 0
    if not count:
        return []
    if count == 1:
        return ["1 answer about these screenshots was kept. It doesn't save any other assignment."]
    return [
        f"{count} answers about these screenshots were kept. They don't save any other assignment."
    ]


def split_waiting(rows: RowsShown, post: ReviewPost) -> bool:
    """Whether this page gives back a split answer its press posted and no press has stored: a
    stored split draws no question, so a question shown with the posted answer checked waits."""
    return any(
        post.choices.get(question.field) and any(checked for _, _, checked in question.options)
        for question in rows.splits
    )


def with_split_waiting(lines: Sequence[str]) -> list[str]:
    """A press's lines with the one line that says where an unstored split answer is kept, worded
    together with the pointer to Save review for later where the page says that pointer."""
    pointer = PRESS_SAID["choices_not_saved"]
    if pointer not in lines:
        return [*lines, PRESS_SAID["split_waits"]]
    together = PRESS_SAID["split_waits_nothing_saved"]
    return [together if line == pointer else line for line in lines]


async def no_form_with_typing(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    said: Sequence[str],
    status: int,
    done: Sequence[str] = (),
    *,
    notes: Sequence[str] = (),
    behind: bool,
    finished: bool = False,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A press's page with no form: an earlier save's result first (``done``), the press's lines,
    kept answers and ``notes`` apart from the rest, the out-of-date notice where the review's stored
    revision isn't its content's (``behind``), every box it typed to copy, a Save's date beside
    carried words among them (``typed_beside_words``), apart from the class, title and
    instructions boxes this request's own save is shown to hold exactly (``saved_exactly``), and
    the ways back. Where a read behind this page says the review finished (``finished``), closed
    or not, the notice is said as a page known to have ended says it, with no instruction to
    check the review or try again, since a finished review takes no press."""
    typed = await typing_by_press(
        request, beside_words=True, as_sent=True, every_press=True, saved_exactly=saved_exactly
    )
    notice = ENDED_SAID[OUT_OF_DATE] if finished else OUT_OF_DATE
    lines = [
        notice if shown == OUT_OF_DATE else shown
        for line in typing_here(said, typed)
        for shown in UNSHOWN_SAID.get(line, (line,))
        if shown != OUT_OF_DATE or not behind
    ]
    kept = list(dict.fromkeys([*apart_lines(lines), *notes]))
    problems = [
        *(line for line in lines if line not in APART_SAID),
        *([notice] if behind else []),
    ]
    if done:
        return saved_outcome(
            request,
            state,
            done,
            None,
            finished=False,
            typed=typed,
            notes=kept,
            problems=problems,
            typed_said=UNSHOWN_TYPED,
            review=f"{PREFIX}/{draft_id}",
        )
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "kept": kept,
            "said": problems,
            "typed": typed,
            "typed_said": UNSHOWN_TYPED,
            "review": f"{PREFIX}/{draft_id}",
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=status,
    )


def no_longer_offered(post: ReviewPost, rows: RowsShown) -> bool:
    """Whether a posted choice's question is on this page without that value among its options."""
    return any(
        field in rows.offered and not rows.offered[field].issuperset(values)
        for field, values in post.choices.items()
    )


def post_off_record(
    post: ReviewPost, own: frozenset[str], held: Mapping[tuple[str, str], str]
) -> bool:
    """Whether a resent press's choices aren't what the record holds: an answer it holds otherwise
    or not at all, or one it holds that the press left blank. A double press's are what its first
    copy kept, whatever that did to the page. Its save's own rows go with that save, and a page's
    default destination sent unchanged is no choice of hers, stored or not."""
    for answer in post.answers:
        if answer.names and own.issuperset(answer.names):
            continue
        if answer.kind == "destination" and post.defaults.get(answer.subject) == answer.value:
            continue
        if held.get((answer.kind, answer.subject)) != answer.value:
            return True
    return any(key in held for key in post.withdrawn)


async def answers_held(state: ApplicationState, draft_id: str) -> dict[tuple[str, str], str]:
    """This review's kept answers, from the record, by kind and subject."""
    record = state.project_state
    student = await asyncio.to_thread(record.student_id)
    tables = await asyncio.to_thread(record.settled, record.draft_for_save, student, draft_id)
    return {key: value for key, (value, _) in tables.answers.items()} if tables else {}


async def choices_said_lost(
    state: ApplicationState,
    draft_id: str,
    post: ReviewPost,
    rows: RowsShown | None,
    own: frozenset[str] | None,
    current: bool,
    behind: bool,
    answers_read: list[Mapping[tuple[str, str], str]] | None = None,
) -> bool:
    """What a resend, a conflict or a refused press can't show. On a page whose form carries its
    choices (``current``, not ``behind``), an answer she cleared that the rows' read holds (beside a
    conflict or a resend, the record), with its question gone. At another version, or past the
    page's revision, on a resend with no conflict, whatever isn't what the record holds (past the
    revision with none cleared, as the rows' own read holds it), or isn't offered. One exception: a
    question gone whose posted choice the record still holds goes unsaid, since nothing tells it
    from one her first copy took off. Past the page's version or revision, the answers its own read
    finds go in ``answers_read``."""
    if rows is None:
        return False
    if current and not behind:
        if not post.withdrawn:
            return False
        if not (post.conflicted or own is not None):
            return any(
                key in rows.held and answer_field(*key) not in rows.offered
                for key in post.withdrawn
            )
        held: Mapping[tuple[str, str], str] = await answers_held(state, draft_id)
        return any(key in held and answer_field(*key) not in rows.offered for key in post.withdrawn)
    if own is None or post.conflicted:
        return False
    if current and not post.withdrawn:
        held = rows.held
    else:
        held = await answers_held(state, draft_id)
        if answers_read is not None:
            answers_read.append(held)
    return post_off_record(post, own, held) or no_longer_offered(post, rows)


async def later_press(
    request: Request, state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> Response:
    """Save review for later: the answers and the typed corrections, in one transaction, the
    corrections only when no other tab saved for later since the page. Once it commits, one read
    of the review decides the answer: the redirect; where it set something "No assigned date was
    given" aside, a page with no form (``later_with_words``) and its confirmation; and where that
    read shows a revision other than the one its commit wrote, a page with no form saying this
    attempt saved the review for later, then the out-of-date notice. Where none of those holds and
    a box was sent other than as the save kept it (``kept_for_later``), in place of the redirect, a
    page with no form saying this attempt saved it and when, giving back only those boxes. Each
    other page with no form gives back as sent every box it typed apart from a class, title or
    instructions box the write is shown to hold exactly (``text_boxes`` of what it kept), the one
    with words after one more read, which answers instead if it finds the review ended or is
    refused; or, with the review ended or its read refused, the typing to copy, less those boxes,
    redirecting past a refused read only when there's nothing to give back. Past a refused read
    the copies owed choose the page and never whether a box comes back (``unshown_page_for``): the
    one saying the review can't be shown, or the one saying this attempt saved it, each box under
    its card's name. A read saying the review ended says the answers were kept before it did only
    when this write stored some."""
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
        review = await state_of_review(state, found, post.corrections)
    except sqlite3.Error as error:
        noted(error)
        return await press_refused(request, state, found.draft_id, post, error, "later_failed")
    post = dataclasses.replace(post, held=None if review is None else review.held)
    ticked = row_keys(review, post.units) if review is not None else frozenset()
    progress = progress_text(post.corrections, ticked)
    try:
        done = await asyncio.to_thread(
            state.import_drafts.save_for_later,
            student,
            found.draft_id,
            version=post.version,
            progress_revision=post.progress_revision,
            answers=list(post.answers),
            progress=progress,
            withdrawn=list(post.withdrawn),
        )
    except ReviewChanged:
        return await with_typing(request, state, found.draft_id, post, [OUT_OF_DATE], 409)
    except (StoreBusy, WriterBusy):
        said = [PRESS_SAID["later_failed"]]
        return await with_typing(request, state, found.draft_id, post, said, 503)
    except DraftClosed:
        return await closed_on_press(request, state, found.draft_id, 409, rows=False)
    except (DraftExpired, DraftNotFound) as error:
        return await refused_keeping_typing(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return await press_refused(
            request, state, found.draft_id, post, error, "later_failed", [UNKNOWN_SAID["later"]]
        )
    if done.progress_saved:
        stored = answers_said(done.answers)
        kept = kept_for_later(
            review, post.corrections, readings_checked=found.version == post.version
        )
        saved_exactly = text_boxes(kept)
        now = await review_state_now(state, found.draft_id)
        if now == "busy":
            owed = await later_copies_owed(request, saved_exactly)
            if unshown_page_for(owed, post.corrections):
                return await unshown_with_typing(
                    request, state, found.draft_id, stored, 200, saved_exactly=saved_exactly
                )
        elif isinstance(now, str):
            return await gone_with_typing(
                request, state, now, stored, 200, saved_exactly=saved_exactly
            )
        elif now.closed_at is not None:
            return await closed_on_press(
                request,
                state,
                found.draft_id,
                200,
                rows=False,
                notes=stored,
                saved_exactly=saved_exactly,
            )
        elif post.dropped:
            return await later_with_words(
                request, state, now, done.progress_revision, stored, saved_exactly=saved_exactly
            )
        elif now.progress_revision != done.progress_revision:
            return await saved_for_later_with_typing(
                request,
                state,
                found.draft_id,
                [THIS_ATTEMPT_SAVED],
                [OUT_OF_DATE],
                saved_exactly=saved_exactly,
            )
        if await typing_by_press(request, shown=kept, every_press=True):
            lines = [THIS_ATTEMPT_SAVED, saved_for_later_at(state)]
            return await saved_for_later_with_typing(
                request, state, found.draft_id, lines, [], kept_as=kept
            )
        lines = [*stored, PRESS_SAID["later"], saved_for_later_at(state)]
        return to_review(state, found.draft_id, lines)
    answered = done.answers.stored > 0
    said = [*answers_said(done.answers), PRESS_SAID["later_behind"]]
    return await with_typing(
        request, state, found.draft_id, post, said, 409, answered=answered, own_write=done.answers
    )


def kept_for_later(
    review: ReviewState | None, corrections: Corrections, *, readings_checked: bool = True
) -> frozenset[tuple[str, str, str]]:
    """Each box a Save for later's committed write is shown to hold exactly, by key, field and
    text. A correction it wrote counts under the text stored, and a removal as an empty box;
    neither needs the review state. A class, title or instructions box with no correction counts
    under its reading's text, line endings made line feeds, only where the review state is in
    hand, was read at the version the write was checked against (``readings_checked``), and every
    open piece of its row with a reading gives that same text. A date box with no correction
    counts as the page draws it."""
    drawn: dict[tuple[str, str], str] = {}
    if review is not None:
        drawn = {
            subject: value or ""
            for subject, value in fold_reads(review).items()
            if not words_box(subject[1])
        }
    if review is not None and readings_checked:
        rows: dict[str, list[dict[str, str]]] = {}
        for piece in review.open:
            read = piece.as_read
            if read is None:
                continue
            texts = {"course": read.course, "title": read.title}
            for index, text in enumerate(read.texts):
                texts[f"instruction:{index}"] = correction_text("instruction:0", text)
            rows.setdefault(piece.key, []).append(texts)
        for key, pieces in rows.items():
            for field, text in pieces[0].items():
                if all(other.get(field) == text for other in pieces):
                    drawn[(key, field)] = text
    for key, fields in corrections.items():
        for field, saved in fields.items():
            drawn[(key, field)] = "" if saved is None else saved
    return frozenset((key, field, text) for (key, field), text in drawn.items())


async def later_copies_owed(
    request: Request, saved_exactly: Collection[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    """Each box a Save for later's post is owed a copy of once its write has committed, by key,
    field and text as sent (``typed_as_sent``): every one apart from a class, title or
    instructions box the write is shown to hold exactly (``saved_exactly``). With no form to
    read, none."""
    try:
        form = await form_of(request)
    except FormUnreadable:
        return []
    items = [(name, value) for name, value in form.multi_items() if isinstance(value, str)]
    return [box for box in typed_as_sent(items) if box not in saved_exactly]


def unshown_page_for(owed: Iterable[tuple[str, str, str]], corrections: Corrections) -> bool:
    """Whether a Save for later that committed and then found its review busy answers on the page
    saying the review can't be shown, and not on the one saying this attempt saved it for later.
    It does where a copy it owes is a date box's or carried words, a class, title or instructions
    box sent with outer whitespace, or such a box the write stored a correction with text for. It
    looks only at the copies owed, so a box the write holds exactly chooses no page, and it
    chooses the page alone: every copy owed comes back on either."""
    return any(
        not words_box(field) or text != canonical(text) or bool(corrections.get(key, {}).get(field))
        for key, field, text in owed
    )


async def later_with_words(
    request: Request,
    state: ApplicationState,
    after: DraftSnapshot,
    revision: int,
    stored: Sequence[str] = (),
    *,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """Save for later that committed with something "No assigned date was given" set aside: no form,
    since another tab can save after any read here and a form from it would carry a revision that
    sends this press's boxes over that save. It says what it did, gives back every correction it
    typed, as sent, to copy, and the ways back, so the next form comes from a load of the review.
    Where its read's revision isn't the one its commit wrote (``revision``), the review was saved
    since, and in place of the confirmation it says this attempt saved it, then that the page is out
    of date. It reads the review once more only to see whether it has ended: a refused read answers
    at the commit's 200 with the answers lines of this write (``stored``) and nothing about
    corrections, and one saying the review ended says those lines as an ended review's page does.
    Its confirmation follows its answers lines. Each page leaves out of its copies the class,
    title and instructions boxes the write is shown to hold exactly (``saved_exactly``)."""
    ended = await ended_under_press(
        request,
        state,
        after.draft_id,
        200,
        stored,
        refused_status=200,
        saved_exactly=saved_exactly,
    )
    if ended is not None:
        return ended
    if after.progress_revision != revision:
        return await saved_for_later_with_typing(
            request,
            state,
            after.draft_id,
            [THIS_ATTEMPT_SAVED],
            [OUT_OF_DATE],
            saved_exactly=saved_exactly,
        )
    lines = [*stored, PRESS_SAID["later"], saved_for_later_at(state)]
    return await saved_for_later_with_typing(
        request, state, after.draft_id, lines, [], saved_exactly=saved_exactly
    )


async def saved_for_later_with_typing(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    kept: Sequence[str],
    said: Sequence[str],
    *,
    kept_as: Collection[tuple[str, str, str]] | None = None,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A Save for later's commit page, with no form: what it says in the status style (``kept``) or
    the problem style (``said``), every correction it typed, as sent, to copy, by its press
    (``typing_by_press``), apart from the class, title and instructions boxes the save is shown to
    hold exactly (``saved_exactly``), and the ways back to the review and to Family review. With
    ``kept_as``, only each box sent other than as the save kept it, labeled by its card. It reads
    no store, signs nothing, and offers nothing that repeats the press."""
    typed = (
        await typing_by_press(request, saved_exactly=saved_exactly)
        if kept_as is None
        else await typing_by_press(request, shown=kept_as, every_press=True)
    )
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "kept": list(kept),
            "said": list(said),
            "typed": typed,
            "typed_said": UNSHOWN_TYPED,
            "review": f"{PREFIX}/{draft_id}",
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=200,
    )


async def save_press(
    request: Request, state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> Response:
    """Save selected homework: a press whose acceptance id is recorded shows that save's outcome;
    otherwise the answers kept at the page's version, then the homework transaction for the
    groups ticked, and closing the review when that finished it. A press that can't ask whether
    it has saved, or that leaves a transaction open, ends at the page with no rows that
    ``unknown_outcome`` makes, with no keep or save after it. Where this press's own values show
    that it wrote no homework, that page and every later look that is refused say so
    (``NO_HOMEWORK_BY_THIS_ATTEMPT``): its first look refused, before any write; its answers
    write refused by a busy store, writer or file, so its homework never began; nothing ticked;
    the read of her name refused, or meeting a transaction left open; and a homework save that
    returned not saved where the check that follows it (``left_open``) answers that no
    transaction is left open. A homework save that returned not saved and left a transaction
    open says that Blossom couldn't tell whether any homework was saved. So does a refused look
    past a homework save that returned not saved where that check was itself refused, past an
    answers write refused as out of date, closed, expired or removed, or by a file error that
    isn't busy, past a name check that can't be read, and past "Already saved". A destination
    chosen on this press selects its group and holds it for Review changes, with nothing written
    or stored for that group."""
    none_written = NO_HOMEWORK_BY_THIS_ATTEMPT
    record = state.project_state
    draft_id = found.draft_id
    try:
        student = await asyncio.to_thread(record.student_id)
        recorded = await asyncio.to_thread(
            record.settled, record.homework_acceptance, student, draft_id, post.acceptance
        )
    except sqlite3.Error as error:
        acceptance_unread(error)
        return await unknown_outcome(request, state, homework=none_written)
    if recorded is not None:
        own = frozenset(name for name, _ in recorded)
        held = frozenset(name for unit in post.units for name in unit.split("+")) - own
        said = [PRESS_SAID["held_back"]] if held else []
        repeated = dataclasses.replace(post, held_back=held | (post.chosen_here - own))
        return await saved_page(request, state, draft_id, post.acceptance, said, repeated, own)
    try:
        unreviewed = await unreviewed_cards(state, found, post)
    except sqlite3.Error as error:
        noted(error)
        return await unshown_unread(request, state, draft_id, error)
    if unreviewed:
        # Checked before any store: no answer and no assignment is written for it.
        return await with_typing(
            request,
            state,
            draft_id,
            dataclasses.replace(post, held_back=post.chosen_here),
            [PRESS_SAID["not_reviewed"]],
            200,
            homework=none_written,
            opened=unreviewed,
            signed_digests=post.digests,
            as_posted=True,
            held_answers=destinations_held(post),
        )
    if not post.units:
        # No tick posted: nothing written and nothing stored, every choice given back as posted. A
        # destination chosen here selects its group, which the held line speaks for.
        selected_here = post.chosen_here - post.left_off
        return await with_typing(
            request,
            state,
            draft_id,
            dataclasses.replace(post, held_back=post.chosen_here),
            [
                PRESS_SAID["held_back" if selected_here else "none_chosen"],
                PRESS_SAID["choices_not_saved"],
            ],
            200,
            homework=none_written,
            as_posted=True,
            held_answers=dataclasses.replace(destinations_held(post), press="review"),
        )
    try:
        written, still_asked, chosen = await units_written(state, found, post)
    except sqlite3.Error as error:
        noted(error)
        return await unshown_unread(request, state, draft_id, error)
    held = frozenset(name for unit in post.units if unit not in written for name in unit.split("+"))
    post = dataclasses.replace(post, held_back=held | post.chosen_here, held_asking=still_asked)
    if not written:
        if post.version != found.version:
            # Another page moved the review under this one: answered as a write refused at its
            # page's version is, whatever a group here would have been held for.
            return await replayed(request, state, draft_id) or await with_typing(
                request,
                state,
                draft_id,
                dataclasses.replace(post, held_back=frozenset(), held_asking=frozenset()),
                [OUT_OF_DATE],
                409,
            )
        # Every selected group is held: nothing written and nothing stored.
        return await with_typing(
            request,
            state,
            draft_id,
            post,
            [PRESS_SAID["held_back"], PRESS_SAID["choices_not_saved"]],
            200,
            homework=none_written,
        )
    open_rows = {row.key for row in found.rows if row.state == "open"}
    leaves_rows_open = bool(open_rows - rows_of_units(written))
    answers, withdrawn = answers_of_written(post, written)
    try:
        kept = await asyncio.to_thread(
            state.import_drafts.keep_answers,
            student,
            draft_id,
            version=post.version,
            answers=answers,
            withdrawn=withdrawn,
        )
    except ReviewChanged:
        return await replayed(request, state, draft_id) or await with_typing(
            request, state, draft_id, post, [OUT_OF_DATE], 409
        )
    except (StoreBusy, WriterBusy):
        busy = [PRESS_SAID["save_failed"]]
        return await replayed(
            request, state, draft_id, busy, notes=busy, homework=none_written
        ) or await with_typing(request, state, draft_id, post, busy, 503, homework=none_written)
    except DraftClosed:
        return await closed_on_press(request, state, draft_id, 409)
    except (DraftExpired, DraftNotFound) as error:
        return await replayed(request, state, draft_id) or await refused_keeping_typing(
            request, state, error
        )
    except sqlite3.Error as error:
        noted(error)
        unknown = [UNKNOWN_SAID["save"], NO_HOMEWORK]
        # A busy file wrote nothing, so this press's answers and homework were not written.
        if busy_file(error):
            busy = [PRESS_SAID["save_failed"]]
            return await replayed(
                request, state, draft_id, busy, notes=busy, homework=none_written
            ) or await press_refused(
                request,
                state,
                draft_id,
                post,
                error,
                "save_failed",
                unknown,
                homework=none_written,
            )
        unsure = [UNKNOWN_SAID["save"]]
        return await replayed(
            request, state, draft_id, unsure, notes=unsure
        ) or await press_refused(request, state, draft_id, post, error, "save_failed", unknown)
    answered = kept.stored > 0
    kept_said = answers_said(kept)
    stored_here = fields_stored(answers, kept)
    try:
        named = await image_names(request, state, found)
    except TransactionLeftOpen:
        # The read of her name comes ahead of the homework save, which is never called.
        return await unknown_outcome(request, state, kept_said, none_written)
    except sqlite3.Error as error:
        noted(error)
        # The answers are kept and this press's homework transaction never began.
        if busy_file(error):
            return await outcome_page(
                request,
                state,
                draft_id,
                NotSaved("busy"),
                answered,
                post,
                own_write=kept,
                stored_here=stored_here,
            )
        said = [*kept_said, NO_HOMEWORK]
        answer = await replayed(request, state, draft_id, kept_said, homework=none_written)
        return answer or await unshown_with_typing(
            request, state, draft_id, said, 503, as_sent=True
        )
    if named is None:
        said = [*kept_said, SAID["no_name_check"]]
        return await with_typing(
            request,
            state,
            draft_id,
            post,
            said,
            500,
            own_write=kept,
            left_answers=True,
            stored_here=stored_here,
        )
    press = SavePress(
        student,
        draft_id,
        kept.version,
        post.acceptance,
        written,
        post.digests,
        post.defaults,
        post.corrections,
        {image.sha256: (image.status, image.form) for image in named[1]},
        named[0],
        progress_revision=post.progress_revision,
        role=role_of(state),
        screenshot_answers={
            (answer.kind, answer.subject): (answer.value, answer.digest)
            for answer in post.answers
            if answer.kind in ABOUT_THE_SCREENSHOTS
        },
        screenshot_answers_withdrawn=frozenset(
            key for key in post.withdrawn if key[0] in ABOUT_THE_SCREENSHOTS
        ),
    )
    try:
        outcome = await asyncio.to_thread(
            save_homework,
            record,
            state.import_drafts,
            press,
            clock=state.real_clock,
            household=state.clock,
        )
    except Exception as error:
        logger.warning("A screenshot save failed: %s", type(error).__name__)
        outcome = NotSaved("failed")
    if isinstance(outcome, Saved):
        stored_here |= {answer_field(*key) for key in outcome.screenshot_answers_kept}
    if isinstance(outcome, Saved) and leaves_rows_open and not outcome.closed:
        outcome = await selection_kept(
            state,
            student,
            found,
            post,
            outcome,
            written=written,
            chosen=chosen,
            stored=[*answers, *answers_kept_by(post, outcome)],
        )
    homework: Sequence[str] = none_written
    if isinstance(outcome, NotSaved):
        open_now = await left_open(state)
        if open_now:
            # Found here by asking, with no refusal of the store's behind it, so this is its one
            # line.
            logger.error(LEFT_OPEN_ON_THE_RECORD)
            return await unknown_outcome(request, state, kept_said)
        if open_now is None:
            homework = (UNKNOWN_SAID["homework"],)
    own_write = answers_left_stored(kept, answers, post, outcome)
    return await outcome_page(
        request,
        state,
        draft_id,
        outcome,
        answered,
        post,
        own_write=own_write,
        homework=homework,
        stored_here=stored_here,
    )


def fields_stored(answers: Iterable[AnswerToKeep], kept: AnswersKept) -> frozenset[str]:
    """The field of each answer a Save's own write holds: every answer it gave that write, apart
    from each one that write dropped as already saved."""
    dropped = kept.dropped_keys or frozenset()
    return frozenset(
        answer_field(answer.kind, answer.subject)
        for answer in answers
        if (answer.kind, answer.subject) not in dropped
    )


def answers_left_stored(
    kept: AnswersKept, answers: Sequence[AnswerToKeep], post: ReviewPost, outcome: SaveOutcome
) -> AnswersKept:
    """What a Save's own write left stored. Where its homework transaction committed, the answers
    that transaction took off with the pieces it saved are not counted, nor one the record held
    before this press or dropped as already saved: the answers lines say only what stays stored."""
    if not isinstance(outcome, Saved):
        return kept
    held = post.held or {}
    fresh = {
        (answer.kind, answer.subject)
        for answer in answers
        if held.get((answer.kind, answer.subject)) != answer.value
    }
    return kept._replace(stored=len(fresh - outcome.consumed - (kept.dropped_keys or frozenset())))


async def units_written(
    state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> tuple[tuple[str, ...], frozenset[str], frozenset[str]]:
    """The ticked groups this Save writes, worked out before any store: those its page showed
    ready, with no destination changed on this press, none sharing a new slot with a destination
    chosen on it (``new_slot_shared``), none whose title a destination chosen on it leaves asked
    (``titles_left_asked``), no Type answer moving the effect its page signed, and, where a
    question was open on the page, with this press's answers leaving none open. Every other
    ticked group is held. Beside them, the cards of each held group that only an unanswered
    question stops (``ReviewPost.held_asking``), and the cards the destinations chosen on this
    press select (``cards_selected``). A page behind the review is left to its stale-record
    refusal."""
    asks = any(
        post.ready_shown.get(unit) == READY_ASKING and not post.chosen_here & set(unit.split("+"))
        for unit in post.units
    )
    review = (
        await state_of_review(
            state, found, post.corrections, dataclasses.replace(post, press="review")
        )
        if asks or (post.chosen_here and post.version == found.version)
        else None
    )
    stopped = (
        post.chosen_here
        | new_slot_shared(review, post.chosen_here)
        | titles_left_asked(review, post.chosen_here)
    )
    unchanged = [unit for unit in post.units if not stopped & set(unit.split("+"))]
    asking = [unit for unit in unchanged if post.ready_shown.get(unit) == READY_ASKING]
    answered: set[str] = set()
    if asking and review is not None:
        answered = {unit_name(unit.unit) for unit in review.validation.units if unit.ready}
    moved = await types_moved(
        state, found, post, [unit for unit in unchanged if type_answered(post, unit)]
    )
    written = tuple(
        unit
        for unit in unchanged
        if unit not in moved
        and (post.ready_shown.get(unit) == READY_WHOLE or (unit in asking and unit in answered))
    )
    still_asked = frozenset(
        name
        for unit in asking
        if unit not in moved and unit not in answered
        for name in unit.split("+")
    )
    return written, still_asked, cards_selected(review, post)


def new_slot_shared(review: ReviewState | None, chosen: frozenset[str]) -> frozenset[str]:
    """The cards on a new slot that a destination in ``chosen`` names too, worked out with the
    press's answers held (``review``): one slot of one class and title is one assignment, so a
    card written alone would leave the other to make a second row. A saved assignment is no new
    slot, and the same number under another title is another assignment."""
    if review is None or not chosen:
        return frozenset()
    pieces = review.by_name

    def slot_of(name: str) -> tuple[str, ...] | None:
        value = review.mapping.get(name)
        if name not in pieces or value is None or dest_kind(value) != "new":
            return None
        return dest_key(pieces[name], value)

    slots = {slot_of(name) for name in chosen} - {None}
    return frozenset(name for name in review.mapping if slot_of(name) in slots)


def titles_left_asked(review: ReviewState | None, chosen: frozenset[str]) -> frozenset[str]:
    """The cards going to a saved assignment whose typed title is asked once the destinations in
    ``chosen`` are held (``review``): the rename its page proposed would leave two assignments of
    one class, title and dates beside the card chosen on this press, so it waits for that answer."""
    if review is None or not chosen:
        return frozenset()
    return frozenset(
        name for entry in review.preview if "title" in entry.open_questions for name in entry.pieces
    )


def cards_selected(review: ReviewState | None, post: ReviewPost) -> frozenset[str]:
    """The cards of each group that holds a card whose destination this press chose, worked out
    with its answers held (``review``), as the page it returns draws them ticked: a group holding
    a card the press unticked (``ReviewPost.left_off``) is not selected."""
    if review is None:
        return frozenset()
    return frozenset(
        name
        for unit in review.validation.units
        if post.chosen_here & set(unit.unit) and not post.left_off & set(unit.unit)
        for name in unit.unit
    )


def type_answered(post: ReviewPost, unit: str) -> bool:
    """Whether this press gives a card in the group a Type answer the record doesn't hold."""
    names = unit.split("+")
    held = post.held or {}
    return any(
        answer.kind == "kind"
        and not answer.subject.startswith("[")
        and held.get((answer.kind, answer.subject)) != answer.value
        and answer_pieces(answer.kind, answer.subject, names)
        for answer in post.answers
    )


async def types_moved(
    state: ApplicationState, found: DraftSnapshot, post: ReviewPost, typed: Sequence[str]
) -> frozenset[str]:
    """The groups in ``typed`` whose digests, with this press's answers held, are none its page
    signed: worked out with its page's default destinations left as defaults, as a loaded page
    signs them, and then held as answers, as a page Review changes made signs them."""
    if not typed or post.version != found.version:
        return frozenset()
    chosen = tuple(
        answer
        for answer in post.answers
        if not (answer.kind == "destination" and post.defaults.get(answer.subject) == answer.value)
    )
    left = set(typed)
    for answers in (chosen, post.answers):
        held = dataclasses.replace(post, press="review", answers=answers)
        review = await state_of_review(state, found, post.corrections, held)
        if review is None:
            return frozenset()
        worked = unit_digests(review)
        left = {
            unit for unit in left if tuple(sorted(post.digests.get(unit, ()))) != worked.get(unit)
        }
        if not left:
            break
    return frozenset(left)


def answers_kept_by(post: ReviewPost, saved: Saved) -> list[AnswerToKeep]:
    """The answers this press posted that its homework transaction stored."""
    return [
        answer
        for answer in post.answers
        if (answer.kind, answer.subject) in saved.screenshot_answers_kept
    ]


def answers_of_written(
    post: ReviewPost, written: Sequence[str]
) -> tuple[list[AnswerToKeep], list[tuple[str, str]]]:
    """What a Save stores ahead of its homework transaction: the answers and withdrawals about the
    cards it writes. Nothing is stored about a card it doesn't write, and a name or split answer
    goes with the homework transaction (``SavePress.screenshot_answers``)."""
    pieces = frozenset(name for unit in written for name in unit.split("+"))
    return (
        [answer for answer in post.answers if stored_by_save(answer.kind, answer.subject, pieces)],
        [
            (kind, subject)
            for kind, subject in post.withdrawn
            if stored_by_save(kind, subject, pieces)
        ],
    )


def stored_by_save(kind: str, subject: str, pieces: frozenset[str]) -> bool:
    """Whether a Save that writes these cards stores this answer ahead of its homework
    transaction."""
    return kind not in ABOUT_THE_SCREENSHOTS and bool(answer_pieces(kind, subject, pieces) & pieces)


async def selection_kept(
    state: ApplicationState,
    student: str,
    found: DraftSnapshot,
    post: ReviewPost,
    outcome: Saved,
    *,
    written: Sequence[str],
    chosen: frozenset[str],
    stored: Iterable[AnswerToKeep],
) -> Saved:
    """A save that committed and left rows open, with what its press selected written into saved
    progress and the revision that write raised the review to: the groups it ticked and the cards
    its chosen destinations select (``chosen``), apart from the rows a reopened page would start
    on a destination the family pointed elsewhere (``starts_pointed_elsewhere``). Nothing is
    written where a plain load selects the same, unless such a row is left out, as a review with
    no saved progress ticks a default by rule. The save stands whatever this write does."""
    cards = chosen | {name for unit in post.units for name in unit.split("+")}
    unwritten = cards - {name for unit in written for name in unit.split("+")}
    try:
        left_out = await starts_pointed_elsewhere(state, student, found, post, unwritten)
        selecting = dataclasses.replace(post, selection=rows_of_units(cards) - left_out)
        if not left_out and selection_settled(selecting, found, stored, rows_of_units(written)):
            return outcome
        revision = await asyncio.to_thread(
            state.import_drafts.keep_selection,
            student,
            found.draft_id,
            progress_revision=outcome.progress_revision,
            selection=selection_of(selecting),
        )
    except (*STORE_REFUSALS, sqlite3.Error) as error:
        logger.warning("A save's selection couldn't be kept: %s", type(error).__name__)
        return outcome
    return outcome if revision is None else dataclasses.replace(outcome, progress_revision=revision)


async def starts_pointed_elsewhere(
    state: ApplicationState,
    student: str,
    found: DraftSnapshot,
    post: ReviewPost,
    cards: frozenset[str],
) -> frozenset[str]:
    """The rows of these cards, left open by a save that committed, whose posted destination is not
    the one a reopened review starts them on, a page default or an earlier kept answer: the posted
    destination isn't stored, and a tick must not stand for a destination the family didn't leave
    the card on. A card with none to start on keeps its tick and asks. Read after the commit, as
    that save can change what a card defaults to, and only where one of these cards posted a
    destination."""
    posted = {
        answer.subject: answer.value
        for answer in post.answers
        if answer.kind == "destination" and answer.subject in cards
    }
    if not posted:
        return frozenset()
    after = await asyncio.to_thread(state.import_drafts.snapshot, student, found.draft_id)
    review = await state_of_review(state, after, await saved_corrections(state, after))
    if review is None:
        return frozenset()
    return rows_of_units(
        name
        for name, value in posted.items()
        if name in review.mapping and value_json(review.mapping[name]) != value
    )


async def left_open(state: ApplicationState) -> bool | None:
    """Whether a save that didn't commit has left its transaction open: its commit and its rollback
    both refused. Then whether it saved isn't known. None where the store refuses to say, which
    is no evidence that one is open and none that none is."""
    try:
        return await asyncio.to_thread(state.project_state.transaction_left_open)
    except sqlite3.Error as error:
        noted(error)
        return None


async def outcome_page(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    outcome: SaveOutcome,
    answered: bool,
    post: ReviewPost,
    *,
    own_write: AnswersKept,
    homework: Sequence[str] = NO_HOMEWORK_BY_THIS_ATTEMPT,
    stored_here: frozenset[str] = frozenset(),
) -> Response:
    """What a Save press answers with, by what committed: "nothing was saved" only when the
    homework transaction didn't commit, and a save that committed is reported as saved, in the
    success pattern, with anything after it that didn't finish. A review that expired, or is
    gone, gives the press's typing back with what it says, never in place of a save that
    committed. A save that couldn't start or finish has no result of its own to show, and another
    copy of the press can have saved since this one last looked, so it looks again: one found
    answers first, and "wasn't saved" is said only when this look finds none. A commit after it goes
    unseen. A save that committed whose page then fails gives its typing back apart from the class,
    title and instructions boxes its evidence holds (``text_boxes``). The answers lines of this
    press's own write (``own_write``, by ``answers_said``) are said beside each line saying why
    no homework was saved, beside "Already saved", on a closed review's page, on the page of a
    look that is refused, and beneath this press's own save where its page doesn't fail and
    that save didn't itself close the review, whose page says no answers line. A review that
    expired or is gone under the homework save says in their place what the write returned
    (``kept_then_ended``). A receipt found by the look past a busy or failed save, and a replayed
    save, show that save's page without them. A look for a receipt that is refused past a
    homework save that returned not saved says this press's homework lines (``homework``). By
    default those say that no homework was saved by this attempt and that Blossom couldn't tell
    whether another attempt saved any (``NO_HOMEWORK_BY_THIS_ATTEMPT``): the homework save never
    began, or the check after it answered that no transaction is left open. Where that check was
    refused, ``save_press`` passes that Blossom couldn't tell whether any homework was saved.
    Past "Already saved" a refused look says that too. Each outcome in ``ANSWERS_COME_BACK`` gives
    the press's answers back on their questions; those it stored (``stored_here``) stand as
    stored."""
    kept = answers_said(own_write)
    if isinstance(outcome, NotSaved):
        if outcome.reason in ("absent", "expired"):
            ended = kept_then_ended(own_write)
            return await gone_with_typing(request, state, outcome.reason, ended, 409)
        if outcome.reason == "closed":
            return await closed_on_press(
                request, state, draft_id, 409, kept, notes=kept, homework=homework
            )
        if outcome.reason in ("busy", "failed"):
            recorded = await replayed(request, state, draft_id, kept, homework=homework)
            if recorded is not None:
                return recorded
        status = 503 if outcome.reason in ("busy", "failed") else 409
        beside_changed = post.held_back and outcome.reason == "changed"
        held_line = [PRESS_SAID["held_back"]] if beside_changed else []
        return await with_typing(
            request,
            state,
            draft_id,
            post,
            [*not_saved(outcome, own_write), *held_line],
            status,
            own_write=own_write,
            homework=homework,
            left_answers=outcome.reason in ANSWERS_COME_BACK,
            stored_here=stored_here,
        )
    if isinstance(outcome, AlreadySaved):
        said = [*kept, PRESS_SAID["already"]]
        return await with_typing(
            request,
            state,
            draft_id,
            post,
            said,
            200,
            own_write=own_write,
            left_answers=answers_left_unstored(post),
            stored_here=stored_here,
        )
    if isinstance(outcome, Replayed):
        own = frozenset(name for name, _ in outcome.pairs)
        return await saved_page(request, state, draft_id, outcome.acceptance_id, [], post, own)
    done = [counted(*outcome.counts)]
    if outcome.already_units:
        done.append(PRESS_SAID["already"])
    problems = [
        *([PRESS_SAID["changed"]] if outcome.changed_units else []),
        *([PRESS_SAID["held_back"]] if post.held_back else []),
    ]
    try:
        return await committed_page(
            request,
            state,
            draft_id,
            post,
            outcome,
            answered,
            done,
            problems,
            own_write,
            stored_here,
        )
    except Exception as error:
        noted_after_commit(error)
        return await saved_busy(
            request,
            state,
            draft_id,
            done,
            problems,
            as_sent=True,
            saved_exactly=text_boxes(outcome.stored),
        )


async def committed_page(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    post: ReviewPost,
    outcome: Saved,
    answered: bool,
    done: list[str],
    problems: Sequence[str],
    own_write: AnswersKept,
    stored_here: frozenset[str] = frozenset(),
) -> Response:
    """A save that committed, by what is known of its review. Where the save couldn't confirm
    whether it finished the review (``incomplete``), one read decides, and the page takes that
    read as it is: rows still open answer as a save that left them open; a finished review says
    the cleanup closes it; a closed one is the finished answer; and a read that fails, or an
    ended review, promises no cleanup, since the cleanup closes only a finished review. A save
    that closed the review itself says its counts as an ended review's page does: its own
    transaction counted what was left, and its close came after, so something can still be
    counted as left on a review that has finished. A closed
    review's copies leave out only text an assignment this save wrote holds exactly in that field,
    as read back inside its transaction (``Saved.stored``; a date by its own piece). A page that
    can't read the review leaves out the class, title and instructions boxes among that evidence
    (``text_boxes``)."""
    if outcome.closed:
        typed = await typing_of(request, outcome.stored, as_sent=True, beside_words=True)
        week = await week_of(state, outcome.pairs)
        lines = [*ended_lines(done, saved=True), *problems]
        return saved_outcome(request, state, lines, week, typed=typed)
    read: DraftSnapshot | None = None
    if outcome.incomplete:
        now = await snapshot_keeping_typing(
            request,
            state,
            draft_id,
            problems,
            200,
            done,
            outcome,
            answers_said(own_write),
            saved_exactly=text_boxes(outcome.stored),
        )
        if isinstance(now, Response):
            return now
        read = now
    return await partly_saved(
        request,
        state,
        draft_id,
        post,
        done,
        problems,
        read,
        outcome,
        answered,
        own_write=own_write,
        stored_here=stored_here,
    )


async def week_of(state: ApplicationState, pairs: Iterable[tuple[str, str]]) -> str | None:
    """Her week at the earliest due date among the assignments a save wrote, with that card in
    view; None when none of them has a due date, or when the assignments can't be read once the
    save has committed, so the saved outcome still shows, with no way to her week. With no
    assignment named, such as when the save's own record can't be read, it reads nothing."""
    targets = {target for _, target in pairs}
    if not targets:
        return None
    try:
        everything = await asyncio.to_thread(state.project_state.all_assignments)
    except STORE_FAILURES as error:
        logger.warning("Her week couldn't be read after a save: %s", type(error).__name__)
        return None
    except Exception as error:
        noted_after_commit(error)
        return None
    dated = sorted(
        (item.due_date, item.assignment_id)
        for item in everything
        if item.assignment_id in targets and item.due_date is not None
    )
    if not dated:
        return None
    day, assignment_id = dated[0]
    return week_href(day, assignment_id)


def saved_outcome(
    request: Request,
    state: ApplicationState,
    lines: Sequence[str],
    week: str | None,
    *,
    finished: bool = True,
    typed: Sequence[tuple[str, str]] = (),
    notes: Sequence[str] = (),
    problems: Sequence[str] = (),
    typed_said: str = TYPED_SAID,
    review: str | None = None,
) -> Response:
    """A save that finished the review, as the grade save's outcome shows one: the steps line
    at Saved, the counts as the panel's heading, her week, and that the review is finished.
    Where whether it finished isn't known (``finished`` false), it isn't said, what else the
    press said (``notes``) is, and the corrections the post typed (``typed``) are given back to
    copy. ``problems`` are said beneath, in the problem style, with no focus of their own, and
    ``review`` adds the way back to the review. A refused Save for later's line in ``notes`` says
    the typing is still here only beside a copy (``typing_here``)."""
    notes = typing_here(notes, typed)
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "said": [SAID["closed"], *notes] if finished else list(notes),
            "problems": list(problems),
            "outcome": list(lines),
            "week": week,
            "typed": list(typed),
            "typed_said": typed_said,
            "review": review,
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=200,
    )


async def saved_finished(
    request: Request,
    state: ApplicationState,
    done: Sequence[str],
    said: Sequence[str],
    pairs: Iterable[tuple[str, str]],
    stored: Iterable[tuple[str, str, str]],
    *,
    closed: bool,
    notes: Sequence[str] = (),
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A save that committed, its review read as finished: what it saved, her week (``pairs``),
    and to copy whatever it typed that the save can't show it stored (``stored``). A closed review
    says it's finished. One not closed says that couldn't be confirmed and the cleanup
    closes it, the one page that promises this, since the cleanup closes only a finished review.
    Lines that point at the review's rows are said as an ended review's page says them, since a
    finished review has none left to check. ``notes`` go beneath the counts, on either. The copies
    leave out, too, the boxes this request's own committed Save for later is shown to hold exactly
    (``saved_exactly``)."""
    typed = await typing_by_press(
        request, stored, as_sent=True, beside_words=True, saved_exactly=saved_exactly
    )
    week = await week_of(state, pairs)
    lines = ended_lines(done, saved=True)
    if closed:
        return saved_outcome(
            request, state, [*lines, *ended_lines(said, saved=True)], week, typed=typed, notes=notes
        )
    return saved_outcome(
        request,
        state,
        lines,
        week,
        finished=False,
        typed=typed,
        notes=notes,
        problems=[PRESS_SAID["incomplete"], *ended_lines(said, saved=True)],
    )


async def partly_saved(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    post: ReviewPost,
    done: list[str],
    said: Sequence[str] = (),
    read: DraftSnapshot | None = None,
    saved: Saved | None = None,
    answered: bool = False,
    own: frozenset[str] | None = None,
    *,
    notes: Sequence[str] = (),
    counted: bool = True,
    own_write: AnswersKept | None = None,
    stored_here: frozenset[str] = frozenset(),
) -> Response:
    """A save that leaves the review open: the outcome in the success style, and the rows left
    open with the corrections the press typed still in the form, marked as not saved, none of
    them stored. A caller that has just read the review passes that read (``read``), and the
    page takes it as it is. A review that expired since answers with the outcome and the typing
    to copy, and one that has finished since answers as a finished review does. Each says the
    answers lines of this request's own save (``own_write``) beneath the outcome, as an ended
    review's page says them where the read says the review ended or finished. What a receipt's
    press kept itself (``notes``, with whether that save has counts: ``counted``) goes as
    ``with_typing`` says, or beneath the outcome if the corrections can't be read. A page here that
    can't read the review leaves out of its copies the class, title and instructions boxes this
    request's own save is shown to hold exactly (``text_boxes``)."""
    saved_exactly = text_boxes(saved.stored) if saved else frozenset()
    its_answers = answers_said(own_write) if own_write is not None else []
    after = read or await snapshot_keeping_typing(
        request, state, draft_id, said, 200, done, saved, its_answers, saved_exactly=saved_exactly
    )
    if isinstance(after, Response):
        return after
    if after.finished:
        pairs, stored = (saved.pairs, saved.stored) if saved else ((), ())
        return await saved_finished(
            request,
            state,
            done,
            said,
            pairs,
            stored,
            closed=False,
            notes=ended_lines(its_answers, saved=True),
        )
    try:
        kept = await saved_corrections(state, after)
    except sqlite3.Error as error:
        noted(error)
        return await saved_busy(
            request,
            state,
            draft_id,
            done,
            said,
            notes=[*its_answers, *notes],
            as_sent=True,
            saved_exactly=saved_exactly,
        )
    unsaved = unsaved_typing(after, post.corrections, kept)
    return await with_typing(
        request,
        state,
        draft_id,
        post,
        list(said),
        200,
        done,
        after,
        saved,
        answered,
        unsaved,
        own,
        notes=notes,
        counted=counted,
        own_write=own_write,
        left_answers=saved is not None and answers_left_unstored(post, saved),
        stored_here=stored_here,
    )


def answers_left_unstored(post: ReviewPost, saved: Saved | None = None) -> bool:
    """Whether a Save posted an answer that neither its own write nor its homework transaction
    (``saved``) stored and the record doesn't hold, or left blank one that stays stored. A page's
    default destination sent unchanged is no choice of hers."""
    written = frozenset(
        name for unit in post.units for name in unit.split("+") if name not in post.held_back
    )
    held = post.held or {}
    taken = (
        saved.screenshot_answers_kept | saved.screenshot_answers_withdrawn
        if saved is not None
        else frozenset()
    )

    def left_open(kind: str, subject: str) -> bool:
        return (kind, subject) not in taken and not stored_by_save(kind, subject, written)

    return any(
        left_open(answer.kind, answer.subject)
        and held.get((answer.kind, answer.subject)) != answer.value
        and not (answer.kind == "destination" and post.defaults.get(answer.subject) == answer.value)
        for answer in post.answers
    ) or any(
        left_open(kind, subject) and (kind, subject) in held for kind, subject in post.withdrawn
    )


def tick_unlike_progress(snapshot: DraftSnapshot, typed: Corrections, kept: Corrections) -> bool:
    """Whether a post's "No assigned date was given" on a row still open differs from saved
    progress (``kept``, as the page's rows read it): ticked where it holds a date or nothing, or no
    progress is saved, unticked where it holds that."""
    still = {row.key for row in snapshot.rows if row.state == "open"}

    def ticked(given: Corrections, key: str) -> bool:
        return "assigned" in given.get(key, {}) and given[key]["assigned"] is None

    return any(
        key.startswith("date:")
        and key.removeprefix("date:").split("|")[0] in still
        and ticked(typed, key) != ticked(kept, key)
        for key in {*typed, *kept}
    )


def tick_beyond_progress(
    snapshot: DraftSnapshot,
    post: ReviewPost,
    kept: Corrections,
    held: Mapping[tuple[str, str], str] | None,
    source: DraftSnapshot | None = None,
    proven: Collection[str] | None = None,
) -> bool:
    """Whether "No assigned date was given" was ticked or set words aside on an open row with no
    correction to none, unless it answers a day heading's confirmation ``held`` holds as posted, in
    ``proven`` if given, by ``source``'s or the snapshot's progress absent or read, no date."""
    still = {row.key for row in snapshot.rows if row.state == "open"}
    posted = {(answer.kind, answer.subject): answer.value for answer in post.answers}
    text = (snapshot if source is None else source).progress
    readable = text is None or progress_read(text) is not None

    def confirmed(key: str) -> bool:
        asked = ("confirm", confirm_subject(key.removeprefix("date:"), "heading"))
        return (
            readable
            and held is not None
            and asked in posted
            and held.get(asked) == posted[asked]
            and (proven is None or key in proven)
            and "assigned" not in kept.get(key, {})
        )

    return any(
        key.removeprefix("date:").split("|")[0] in still
        and post.corrections.get(key, {}).get("assigned", "") is not None
        and not confirmed(key)
        for key in no_date_keys(post)
    )


def words_beside_unlike(
    snapshot: DraftSnapshot,
    post: ReviewPost,
    held: Mapping[tuple[str, str], str] | None,
    progress: Corrections | None,
    newer: DraftSnapshot | None = None,
) -> bool:
    """Whether a post that set words aside, or ticked "No assigned date was given" alone, holds a
    choice the answers read (``held``) or saved progress don't hold, or has none to compare with: a
    later read's (``newer``) if made, else its snapshot's as the rows' read worked it out
    (``progress``)."""
    if not no_date_keys(post) and not no_date_ticked(post.corrections):
        return False
    if newer is None:
        kept = progress_in_hand(snapshot) if progress is None else progress
    else:
        kept = progress_in_hand(newer)
        if kept is None:
            proofs = shape_three_for_its_piece(snapshot, newer, post, progress)
            return tick_unlike_progress(
                snapshot, post.corrections, ticks_of_shape_three()
            ) or tick_beyond_progress(snapshot, post, proofs, held, newer, proven=proofs)
    return (
        kept is None
        or tick_unlike_progress(snapshot, post.corrections, kept)
        or tick_beyond_progress(snapshot, post, kept, held, newer)
    )


def no_date_ticked(typed: Corrections) -> bool:
    """Whether these corrections tick "No assigned date was given" on any piece."""
    return any(
        key.startswith("date:") and "assigned" in fields and fields["assigned"] is None
        for key, fields in typed.items()
    )


def no_date_keys(post: ReviewPost) -> tuple[str, ...]:
    """Each piece's dates key where this post's "No assigned date was given" set something aside
    or was ticked, a lone tick confirming a day heading included."""
    ticked = (
        str(json.loads(field.removeprefix("none:"))[0])
        for field, values in post.choices.items()
        if field.startswith("none:") and values == ("none",)
    )
    return tuple(dict.fromkeys([*(key for key, _, _ in post.dropped), *ticked]))


def written_answers(post: ReviewPost, kept: AnswersKept) -> dict[tuple[str, str], str] | None:
    """The answers a press's committed write holds as posted: each one it posted whose key the
    write didn't drop; None where the write's dropped keys aren't known."""
    if kept.dropped_keys is None:
        return None
    return {
        (a.kind, a.subject): a.value
        for a in post.answers
        if (a.kind, a.subject) not in kept.dropped_keys
    }


def ticks_of_shape_three() -> Corrections:
    """The "No assigned date was given" choices valid shape 3 progress holds or gives: none, as
    ``_kept`` refuses a null there and ``shape_four`` gives ``box_corrections`` no tick to keep."""
    return {}


def shape_three_for_its_piece(
    snapshot: DraftSnapshot, newer: DraftSnapshot, post: ReviewPost, converted: Corrections | None
) -> Corrections:
    """The rows' read's conversion of shape 3 progress (``converted``) for a later read's, piece
    by piece: each day heading's confirmation posted on a piece read with no assigned date, both
    reads holding the same progress text and readings, gets its own piece's; none otherwise."""
    if converted is None or newer.progress != snapshot.progress:
        return {}
    if newer.readings != snapshot.readings:
        return {}
    posted = {(answer.kind, answer.subject) for answer in post.answers}
    return {
        key: converted.get(key, {})
        for key in no_date_keys(post)
        if ("confirm", confirm_subject(key.removeprefix("date:"), "heading")) in posted
        and post.corrections.get(key, {}).get("assigned", "") is not None
    }


def progress_in_hand(snapshot: DraftSnapshot) -> Corrections | None:
    """The corrections saved progress keeps, as ``saved_corrections`` gives them, from the
    snapshot's own text with no read: None for progress of shape 3 with corrections."""
    read = progress_read(snapshot.progress)
    if read is None:
        return {}
    return read.corrected if read.shape == PROGRESS_SHAPE or not read.corrected else None


def unsaved_typing(
    snapshot: DraftSnapshot, typed: Corrections, kept: Corrections
) -> frozenset[tuple[str, str]]:
    """The corrections a press posted for a row still open that saved progress (``kept``, as
    ``saved_corrections`` reads it) doesn't hold, and those it holds that the press put back,
    by key and field. A row a save just took has nothing unsaved left."""
    still = {row.key for row in snapshot.rows if row.state == "open"}
    both = {
        (key, field)
        for given in (typed, kept)
        for key, fields in given.items()
        for field in fields
        if key.removeprefix("date:").split("|")[0] in still
    }
    return frozenset(
        (key, field)
        for key, field in both
        if (field in typed.get(key, {}), typed.get(key, {}).get(field))
        != (field in kept.get(key, {}), kept.get(key, {}).get(field))
    )


async def replayed(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    kept: Sequence[str] = (),
    *,
    rows: bool = True,
    notes: Sequence[str] = (),
    closed_seen: bool = False,
    saved_exactly: Collection[tuple[str, str, str]] = (),
    homework: Sequence[str] = (UNKNOWN_SAID["homework"],),
) -> Response | None:
    """A press resent after its review closed: the outcome its acceptance id recorded for
    this review, when it recorded one; else None, so an id recorded for another review gets
    this one's own answer. With ``rows`` false, that outcome's page shows no rows, and ``notes`` go
    on that outcome's page, as ``saved_page`` says, ``closed_seen`` telling it that this press has
    already found the review closed, with Save's busy sentence as ``beneath_a_result`` gives it. A
    Save press whose read is refused, or finds a transaction left open, can't ask whether a save
    is on record, and says what this press knows of its answers (``kept``), then its homework
    lines (``homework``), as ``unknown_outcome`` says them: by default that it couldn't tell
    whether any homework was saved, and what its caller passes where the caller's own values show
    this press wrote none. Both refusals say the same words; the log tells them apart
    (``acceptance_unread``). Any other press has no save of its own to be unsure of, so a refused
    read gets it its own answer. ``saved_exactly`` goes on to the outcome's page."""
    if not opaque(draft_id):
        return None
    form = await form_of(request)
    acceptance = next((v for n, v in form.multi_items() if n == "acceptance"), "")
    if not isinstance(acceptance, str) or not opaque(acceptance):
        return None
    record = state.project_state
    try:
        student = await asyncio.to_thread(record.student_id)
        recorded = await asyncio.to_thread(
            record.settled, record.homework_acceptance, student, draft_id, acceptance
        )
    except sqlite3.Error as error:
        if await save_pressed(request):
            acceptance_unread(error)
            return await unknown_outcome(request, state, kept, homework)
        noted(error)
        return None
    if recorded is None:
        return None
    own = frozenset(name for name, _ in recorded)
    return await saved_page(
        request,
        state,
        draft_id,
        acceptance,
        [],
        own=own,
        rows=rows,
        notes=beneath_a_result(notes),
        closed_seen=closed_seen,
        saved_exactly=saved_exactly,
    )


async def saved_page(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    acceptance: str,
    said: list[str],
    post: ReviewPost | None = None,
    own: frozenset[str] | None = None,
    *,
    rows: bool = True,
    notes: Sequence[str] = (),
    closed_seen: bool = False,
    saved_exactly: Collection[tuple[str, str, str]] = (),
) -> Response:
    """A resent press with a confirmed receipt for this student, this review and this acceptance
    id, which every way here has: a read that found no transaction open, or the save's own read
    in a transaction that then committed. So its counts are committed rows, which no transaction
    left open since rewrites, and a plain read of them holds. That save's counts, and nothing
    written. A review it finished says so. With the review still open and ``rows`` true, the press
    gets its typing back in the review, as the save it repeats did, its post read here when the
    caller had none. A post whose read is refused, or with ``rows`` false one that reads, gets no
    form: the counts, or that they can't be shown, that the review can't be shown, and its typing to
    copy (``saved_busy``). What this press's own write kept (``notes``) is never among that earlier
    save's counts. Where rows are drawn, and on the review a redirect leads to, it's in the page's
    status block of the press's lines (``said``), ahead of "Not saved yet", as a Save's own page has
    it. On any other page it's beneath the counts in the note style, outside the problem alert. It
    stays as the press's reads have it, and goes as an ended review's page says it where this
    page's own read, or the read a press with nothing ticked makes after its rows come back
    empty, says that (``kept_past_empty_rows``). With no counts and a closed read, this page's,
    that one after its rows come back empty or one before it in this press (``closed_seen``), it
    goes unsaid, since a close deletes the review's answers. A line in ``notes`` saying this
    press's write failed or
    can't be told stays on every page here, as it doesn't say anything was kept. A review the store
    refuses to read can't say whether it finished: the
    counts show in the success panel with no word of that, and every correction the post typed
    is given back to copy; for a busy store, with the line that the review can't be shown and
    the way back to it (``saved_busy``), and for a review known to have expired, or to be gone,
    with that said beneath the counts as a refused press does. Counts the household's file
    refuses to read are said not to be shown, the save itself acknowledged, in the same
    pattern (``saved_busy``); a review it refuses to read is said as for a busy store. Its
    acceptance on record means the press committed, so an unexpected error keeps the save's
    result the same way (``noted_after_commit``). What that save saved (``own``, its pieces) goes
    with it, unsaid, on the page that carries the press's choices. With no post to read, past its
    ceiling or not matching its page, the counts come with the typing to copy, by its press
    (``typing_by_press``), and redirect only when there's nothing to give back. The earlier
    save's receipt leaves no box out of a page's copies; this request's own committed Save for
    later does, on each page here with no form, for the class, title and instructions boxes it
    is shown to hold exactly (``saved_exactly``)."""
    record = state.project_state
    try:
        student = await asyncio.to_thread(record.student_id)
        counts = await asyncio.to_thread(record.homework_counts, student, draft_id, acceptance)
    except Exception as error:
        noted_after_commit(error)
        return await saved_busy(
            request,
            state,
            draft_id,
            [*said, COUNTS_UNSHOWN],
            notes=notes,
            as_sent=True,
            saved_exactly=saved_exactly,
        )
    lines = [*said, counted(*counts)] if counts else said
    if closed_seen and not counts:
        notes = unconfirmed_lines(notes)
    try:
        gone: list[str] = []
        busy = False
        try:
            snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
        except (DraftExpired, DraftNotFound) as error:
            snapshot, gone = None, [SAID[gone_by(error)]]
        except (StoreBusy, WriterBusy, sqlite3.Error) as error:
            noted(error)
            snapshot, busy = None, True
        except STORE_REFUSALS:
            snapshot, busy = None, True
        if snapshot is None or snapshot.closed_at is not None or snapshot.finished:
            closed = snapshot is not None and snapshot.closed_at is not None
            if not busy:
                notes = ended_lines(notes, saved=True)
            if not counts:
                if closed:
                    notes = unconfirmed_lines(notes)
                return await saved_busy(
                    request,
                    state,
                    draft_id,
                    lines or [COUNTS_UNSHOWN],
                    notes=notes,
                    as_sent=True,
                    saved_exactly=saved_exactly,
                )
            try:
                pairs = await asyncio.to_thread(
                    record.homework_acceptance, student, draft_id, acceptance
                )
            except sqlite3.Error as error:
                noted(error)
                pairs = None
            if snapshot is not None:
                return await saved_finished(
                    request,
                    state,
                    lines,
                    [],
                    pairs or (),
                    (),
                    closed=closed,
                    notes=notes,
                    saved_exactly=saved_exactly,
                )
            week = await week_of(state, pairs or ())
            if busy:
                return await saved_busy(
                    request,
                    state,
                    draft_id,
                    lines,
                    [],
                    week,
                    notes=notes,
                    as_sent=True,
                    saved_exactly=saved_exactly,
                )
            return await saved_unread(
                request,
                state,
                ended_lines(lines, saved=True),
                [*notes, *gone],
                week,
                saved_exactly=saved_exactly,
            )
        if post is None:
            try:
                post, _ = await review_post(request, state, snapshot)
            except ListTooLarge:
                post = None
            except sqlite3.Error as error:
                noted(error)
                return await saved_busy(
                    request,
                    state,
                    draft_id,
                    lines or [COUNTS_UNSHOWN],
                    notes=notes,
                    as_sent=True,
                    saved_exactly=saved_exactly,
                )
        if post is not None and not rows:
            return await saved_busy(
                request,
                state,
                draft_id,
                lines or [COUNTS_UNSHOWN],
                notes=notes,
                saved_exactly=saved_exactly,
            )
        if post is not None:
            return await partly_saved(
                request,
                state,
                draft_id,
                post,
                lines or [COUNTS_UNSHOWN],
                read=snapshot,
                own=own,
                notes=notes,
                counted=bool(counts),
            )
        typed = await typing_by_press(
            request, as_sent=True, beside_words=True, saved_exactly=saved_exactly
        )
        if typed:
            return saved_outcome(
                request,
                state,
                lines or [COUNTS_UNSHOWN],
                None,
                finished=False,
                typed=typed,
                notes=notes,
                typed_said=UNSHOWN_TYPED,
                review=f"{PREFIX}/{draft_id}",
            )
        return to_review(state, draft_id, notes, done=lines or [COUNTS_UNSHOWN])
    except Exception as error:
        noted_after_commit(error)
        return await saved_busy(
            request,
            state,
            draft_id,
            lines or [COUNTS_UNSHOWN],
            notes=notes,
            as_sent=True,
            saved_exactly=saved_exactly,
        )


# ------------------------------------------------------------------ find under another name

MOST_FOUND: Final = 20
"""The most assignments one search shows."""
MOST_WORDS: Final = 200
"""The most characters of a search's words read."""


@router.post("/{draft_id}/find")
async def find(request: Request, draft_id: str, state: State) -> Response:
    """Find under another name for one open card: saved assignments whose class and title hold
    every word typed, each a whole change posted as Keep these answers. The card and the words
    come in the body, so the address holds only the review's id."""
    found = await snapshot_or_page(request, state, draft_id)
    if isinstance(found, Response):
        return found
    carrying = frozenset({SELECTION_SHOWN, SELECTION_CHECK})
    posted, whole = await fields_of(
        request,
        frozenset({"piece", "words"}) | carrying,
        may_be_absent=frozenset({"words"}) | carrying,
    )
    if not whole:
        return said_page(request, marks_of(state), FIND_SAID["gone"], 400)
    piece_name = posted.get("piece", "")
    words = posted.get("words", "")[:MOST_WORDS]
    shown = posted.get(SELECTION_SHOWN, "")
    signed = [("piece", piece_name), (SELECTION_SHOWN, shown)]
    carried = (
        ((SELECTION_SHOWN, shown), (SELECTION_CHECK, posted[SELECTION_CHECK]))
        if SELECTION_CHECK in posted
        and same_signature(posted[SELECTION_CHECK], page_check(found.page_key, signed))
        else ()
    )
    try:
        review = await state_of_review(state, found, await saved_corrections(state, found))
    except sqlite3.Error as error:
        noted(error)
        return unread_page(request, state, error)
    piece = None if review is None else review.by_name.get(piece_name)
    if review is None or piece is None or piece.name in review.saved:
        return said_page(request, marks_of(state), FIND_SAID["gone"], 404)
    places = {image.sha256: index + 1 for index, image in enumerate(found.images)}
    terms = words.casefold().split()
    listed = {str(e.stands_for) for e in review.entries.get(piece.name, []) if e.stands_for}
    rows = sorted(
        (
            row
            for row in review.rows.values()
            if terms
            and row.assignment_id not in listed
            and all(term in f"{row.course} {row.title}".casefold() for term in terms)
        ),
        key=lambda r: (r.due is None, r.due, r.title, r.assignment_id),
    )[:MOST_FOUND]
    labels = labels_for(rows)
    results = []
    for row in rows:
        fields = [
            ("version", str(found.version)),
            ("press", "keep"),
            (answer_field("destination", piece.name), value_json({"found": row.assignment_id})),
            *carried[:1],
        ]
        fields.append(("offer", page_check(found.page_key, fields)))
        results.append({"label": f"{row.course}: {labels[row.assignment_id]}", "fields": fields})
    return templates.TemplateResponse(
        request,
        "import_find.html",
        {
            "marks": marks_of(state),
            "page": "parent",
            "draft_id": draft_id,
            "piece": piece.name,
            "card": piece_text(places, piece),
            "words": words,
            "carried": carried,
            "results": results,
            "nothing": FIND_SAID["nothing"] if terms and not results else None,
        },
    )


FIND_SAID: Final = {
    "gone": "That card isn't open in this review.",
    "nothing": "Nothing saved matches those words.",
}
"""What the find page says when there is no card to find for, or nothing matches."""


# ------------------------------------------------------------------ setting aside and discarding

AFTER_SAID: Final = {
    "set_aside": "Set aside. It won't be saved from this review.",
    "finished": "This review is finished; there's nothing more to review.",
    "nothing_saved": "Nothing was saved from this review.",
    "discarded": "This review is discarded.",
    "safe": "Anything you already saved is safe.",
    "not_hers": 'You answered "Not hers". Nothing was saved.',
    "not_hers_after_a_save": (
        'Part of this review is already saved, so it can\'t be answered "Not hers". Leave the '
        "rest unsaved instead."
    ),
    "nothing_set_aside": "Nothing was set aside.",
    "nothing_discarded": "Nothing was discarded.",
    "busy": "Blossom was busy, so nothing changed. Try again.",
    "unchecked": "Nothing changed: this page's button couldn't be checked. Try again.",
}
"""What setting aside, finishing and discarding say, all in one place."""
SMALL_FIELDS: Final = {
    "set-aside": frozenset({"version", "keys", "check"}),
    "finish": frozenset({"version", "keys", "check"}),
    "discard": frozenset({"version", "check"}),
    "not-hers": frozenset({"version", "check"}),
}
"""The fields each small form of the review sends, every one of them signed."""


async def small_form(
    request: Request, state: ApplicationState, found: DraftSnapshot, action: str
) -> tuple[int, list[str], str | None] | None:
    """A small form's version and keys, with the selection its page showed where Leave this group
    unsaved carries one, when its fields are whole and signed with the review's page key; None
    when they aren't."""
    carrying = frozenset({SELECTION_SHOWN}) if action == "set-aside" else frozenset()
    fields, whole = await fields_of(
        request, SMALL_FIELDS[action] | carrying, may_be_absent=carrying
    )
    if not whole or not fields.get("version", "").isdigit() or len(fields["version"]) > 12:
        return None
    signed = [("version", fields["version"])]
    if "keys" in SMALL_FIELDS[action]:
        signed.append(("keys", fields["keys"]))
    if action != "set-aside":
        signed.append(("action", action))
    if SELECTION_SHOWN in fields:
        signed.append((SELECTION_SHOWN, fields[SELECTION_SHOWN]))
    if not same_signature(fields["check"], page_check(found.page_key, signed)):
        return None
    try:
        keys = json.loads(fields.get("keys", "[]"))
    except ValueError:
        return None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        return None
    return int(fields["version"]), keys, fields.get(SELECTION_SHOWN)


async def selection_left(
    state: ApplicationState,
    student: str,
    found: DraftSnapshot,
    shown: str | None,
    left: Collection[str],
) -> None:
    """Saved progress given the selection a page showed, less the rows Leave this group unsaved
    set aside from it: an empty selection is never written, and nothing is written where the
    review moved past that page or a plain load shows that selection. The rows stand set aside
    whatever this write does."""
    read = None if shown is None else selection_read(shown, open_row_keys(found))
    if read is None or not read[1] - set(left):
        return
    revision, selected = read
    if await load_shows(state, student, found.draft_id, selected - set(left)):
        return
    try:
        await asyncio.to_thread(
            state.import_drafts.keep_selection,
            student,
            found.draft_id,
            progress_revision=revision,
            selection=lambda progress: selection_text(progress, selected - set(left)),
        )
    except (*STORE_REFUSALS, sqlite3.Error) as error:
        logger.warning("Leaving a group couldn't keep the selection: %s", type(error).__name__)


def unchanged(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    error: sqlite3.Error,
    unknown: str | None = None,
) -> Response:
    """A small form's press refused by the household's file: a busy file changed nothing, as a
    busy store's note on the review says; any other refusal, at a read before the press's write,
    as the review that can't be shown, and at the write, as a note of the press's own sentence
    that whether its change was made isn't known (``unknown``)."""
    if busy_file(error):
        return to_review(state, draft_id, [AFTER_SAID["busy"]])
    if unknown is not None:
        return to_review(state, draft_id, [unknown])
    return said_page(request, marks_of(state), REVIEW_UNAVAILABLE, 503)


def finished_said(done: Closing, *first: str) -> list[str]:
    """What a press that closed the review says: whether anything of it was saved, as its own
    transaction read it."""
    return [*first, AFTER_SAID["finished"] if done.saved else AFTER_SAID["nothing_saved"]]


async def closing_press(
    request: Request, state: ApplicationState, draft_id: str, action: str
) -> Response:
    """Leave this group unsaved, or Finish without the rest: the rows the page named set aside
    at its version, the review closing when nothing is left open. What it says comes from its
    own transaction, never from a read before it or after it."""
    found = await snapshot_or_page(request, state, draft_id)
    if isinstance(found, Response):
        return found
    posted = await small_form(request, state, found, action)
    if posted is None:
        return to_review(state, draft_id, [AFTER_SAID["unchecked"]])
    version, keys, shown = posted
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
    except sqlite3.Error as error:
        noted(error)
        return unchanged(request, state, draft_id, error)
    store = state.import_drafts
    try:
        if action == "set-aside":
            done = await asyncio.to_thread(
                store.set_aside, student, draft_id, version=version, keys=keys
            )
        else:
            done = await asyncio.to_thread(
                store.discard_rest, student, draft_id, version=version, keys=keys
            )
    except ReviewChanged:
        return to_review(state, draft_id, [OUT_OF_DATE, AFTER_SAID["nothing_set_aside"]])
    except (StoreBusy, WriterBusy):
        return to_review(state, draft_id, [AFTER_SAID["busy"]])
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return store_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return unchanged(request, state, draft_id, error, UNKNOWN_SAID[action])
    first = [AFTER_SAID["set_aside"]] if action == "set-aside" else []
    if done.closed:
        lines = finished_said(done, *first)
        return said_page(request, marks_of(state), lines[0], 200, *lines[1:])
    await selection_left(state, student, found, shown, keys)
    return to_review(state, draft_id, first or [AFTER_SAID["set_aside"]])


@router.post("/{draft_id}/set-aside")
async def set_aside(request: Request, draft_id: str, state: State) -> Response:
    """Leave this group unsaved: the group's rows set aside at the page's version."""
    return await closing_press(request, state, draft_id, "set-aside")


@router.post("/{draft_id}/finish")
async def finish(request: Request, draft_id: str, state: State) -> Response:
    """Finish without the rest: every open row the page showed set aside, the images with no
    reading removed, and the review closed when nothing is left open."""
    return await closing_press(request, state, draft_id, "finish")


async def discarding_press(
    request: Request, state: ApplicationState, draft_id: str, action: str
) -> Response:
    """Discard the review, or Not hers: the whole review discarded at the page's version, its
    images, readings and saved progress removed. Not hers only while nothing of it is saved,
    as the discard's own transaction reads it; what Discard says of saved homework comes from
    that transaction too."""
    found = await snapshot_or_page(request, state, draft_id)
    if isinstance(found, Response):
        return found
    posted = await small_form(request, state, found, action)
    if posted is None:
        return to_review(state, draft_id, [AFTER_SAID["unchecked"]])
    try:
        student = await asyncio.to_thread(state.project_state.student_id)
    except sqlite3.Error as error:
        noted(error)
        return unchanged(request, state, draft_id, error)
    try:
        done = await asyncio.to_thread(
            state.import_drafts.discard_review,
            student,
            draft_id,
            version=posted[0],
            refuse_if_saved=action == "not-hers",
        )
    except DraftFrozen:
        return to_review(state, draft_id, [AFTER_SAID["not_hers_after_a_save"]])
    except ReviewChanged:
        return to_review(state, draft_id, [OUT_OF_DATE, AFTER_SAID["nothing_discarded"]])
    except (StoreBusy, WriterBusy):
        return to_review(state, draft_id, [AFTER_SAID["busy"]])
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return store_refusal(request, state, error)
    except sqlite3.Error as error:
        noted(error)
        return unchanged(request, state, draft_id, error, UNKNOWN_SAID[action])
    if action == "not-hers":
        return said_page(request, marks_of(state), AFTER_SAID["not_hers"], 200)
    last = AFTER_SAID["safe"] if done.saved else AFTER_SAID["nothing_saved"]
    return said_page(request, marks_of(state), AFTER_SAID["discarded"], 200, last)


@router.post("/{draft_id}/discard")
async def discard(request: Request, draft_id: str, state: State) -> Response:
    """Discard the review."""
    return await discarding_press(request, state, draft_id, "discard")


@router.post("/{draft_id}/not-hers")
async def not_hers(request: Request, draft_id: str, state: State) -> Response:
    """Not hers: the review discarded, while nothing of it is saved."""
    return await discarding_press(request, state, draft_id, "not-hers")


# ------------------------------------------------------------------ the entry in Family review

ASIDE: Final = "Pasting the school's text stays the exact way on a computer."
"""What Family review says beside Paste school text while screenshots are offered."""
SUMMARY: Final = (
    "Up to 10 screenshots per review, PNG or JPEG, 12 MB each. Screenshots are sent to "
    "Anthropic's API only when you press Read."
)
"""What Family review shows above its screenshot picker: the limits, and what is sent and when."""
DISCLOSURE: Final = (
    "When you press Read, Blossom sends the screenshots to Anthropic's API to be read. A "
    "review's screenshots, what Blossom read from them, and any progress saved for later are "
    "removed from Blossom's database file when the review finishes, is discarded, or expires, "
    "48 hours after it starts. The removal can wait for Blossom's next hourly cleanup, or its "
    "next start. It doesn't guarantee they are erased from the disk, and a backup taken while "
    "the review was open may still hold copies."
)
"""What Family review folds under How screenshots are kept: what is sent and when, what is
removed and when, and the backup caveat."""


def screenshot_entry(request: Request, state: ApplicationState) -> dict[str, object] | None:
    """Family review's screenshot entry: None while screenshot import is off; else the form
    that starts a review, or where screenshots can be added from, and the reviews still open,
    each with when it stops being available."""
    if not state.settings.screenshot_import:
        return None
    if not state.settings.household_sign_in and not from_this_computer(request):
        return {"aside": ASIDE, "only_here": SAID["only_here"], "reviews": []}
    student = state.project_state.student_id()
    try:
        drafts = state.import_drafts.open_drafts(student)
    except (StoreBusy, WriterBusy):
        drafts = ()
    reviews = [
        {
            "href": f"{PREFIX}/{draft.draft_id}",
            "label": f"Homework screenshots from {until_text(state, draft.created_at)}",
            "until": f"Available until {until_text(state, draft.expires_at)}",
        }
        for draft in drafts
        if draft.kind == HOMEWORK
    ]
    return {
        "aside": ASIDE,
        "only_here": None,
        "summary": SUMMARY,
        "disclosure": DISCLOSURE,
        "token": new_creation_token(),
        "issued": state.real_clock.now().isoformat(),
        "reviews": reviews,
    }
