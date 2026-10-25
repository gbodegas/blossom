# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Homework screenshots added in Family review: the upload, the images, and a Read press.

Every route serves a parent alone: with the sign-in on the household gate turns away her and
anyone not signed in, and with it off only this computer is served. No route takes a form
parameter, so that check runs before any byte of a body is read. Every answer is marked
no-store, sign-in on or off. Addresses carry a review's id and an image's id, nothing else.

An upload's body is read here, through ``uploads.read_upload``, within every limit; its images
are prepared before the draft store's one write. A committed upload answers with a redirect to
its review, so a reload sends nothing again; what that upload refused or found already there is
said once on the review, from a short signed note kept in a cookie for that review alone.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Final, NamedTuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from blossom.anthropic_client import model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.household import UnreadableHouseholdSecret
from blossom.routes.forms import FormUnreadable, fields_of, form_of
from blossom.routes.grades import from_this_computer, grade_name_key
from blossom.routes.navigation import week_href
from blossom.screenshot_page import (
    NEEDED,
    ImageName,
    RowsShown,
    answer_field,
    change_said,
    labels_for,
    page_check,
    piece_text,
    rows_shown,
    same_signature,
    signed_action,
)
from blossom.screenshot_page import SAID as PAGE_SAID
from blossom.screenshot_posts import (
    PROGRESS_SHAPE,
    PostRefused,
    ReviewPost,
    progress_read,
    progress_text,
    read_change,
    read_review,
    shape_four,
    typed_back,
)
from blossom.screenshot_reading import read_screenshots
from blossom.screenshot_review import Corrections, change_told, value_json
from blossom.screenshot_save import (
    AlreadySaved,
    NotSaved,
    Replayed,
    ReviewState,
    SaveOutcome,
    SavePress,
    review_state,
    save_homework,
    student_line,
)
from blossom.screenshots import homework_assembly
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.homework_saves import DraftForSave
from blossom.stores.import_drafts import (
    DRAFT_LIFETIME,
    MOST_IMAGES_PRESENT,
    TOKEN_SHAPE,
    Added,
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


router = APIRouter(prefix=PREFIX, dependencies=[Depends(screenshots_here)], include_in_schema=False)
State = Annotated[ApplicationState, Depends(get_application_state)]


class NoStoreForImports:
    """Mark every answer to an import address no-store, whatever made it: a route, a refusal
    handler or a missing address."""

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


def store_refusal(request: Request, state: ApplicationState, error: Exception) -> Response:
    """A store's refusal of an upload or a press, as its page says it."""
    marks = marks_of(state)
    if isinstance(error, TooManyImages):
        return said_page(request, marks, too_many(error.free), 409)
    if isinstance(error, DraftClosed):
        return said_page(request, marks, SAID["closed"], 409)
    if isinstance(error, DraftExpired):
        return said_page(request, marks, SAID["expired"], 410)
    if isinstance(error, DraftFrozen):
        return said_page(request, marks, SAID["frozen"], 409)
    if isinstance(error, StoreBusy | WriterBusy):
        return said_page(request, marks, SAID["busy"], 503)
    return said_page(request, marks, SAID["absent"], 404)


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


async def gone_with_typing(
    request: Request, state: ApplicationState, gone: str, said: Sequence[str], status: int
) -> Response:
    """A press on a review that expired, or that the sweep took away: what the press did first, if
    anything committed, then why the review can't take more, and every correction the post
    typed in a box to copy, read from the post alone. Nothing of the review is shown, nothing
    is stored, and nothing more can be saved from it."""
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "said": [*said, SAID[gone]],
            "typed": await typing_of(request),
            "typed_said": TYPED_SAID,
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=status,
    )


async def typing_of(request: Request) -> list[tuple[str, str]]:
    """Every correction a press's post typed, labeled, read from the post alone (``typed_back``),
    to give back in boxes to copy where the review can't keep it."""
    try:
        form = await form_of(request)
    except FormUnreadable:
        return []
    items = [(name, value) for name, value in form.multi_items() if isinstance(value, str)]
    return [(TYPED_LABELS[field.partition(":")[0]], text) for field, text in typed_back(items)]


async def saved_unread(
    request: Request,
    state: ApplicationState,
    done: Sequence[str],
    said: Sequence[str] = (),
    week: str | None = None,
) -> Response:
    """A save that committed whose review the store refuses to read: what it saved, in the
    success panel whose heading takes the focus, with what else the press said beneath it and
    no word that the review is finished, since that isn't known, and every correction the press
    typed given back to copy."""
    typed = await typing_of(request)
    return saved_outcome(request, state, done, week, finished=False, typed=typed, notes=said)


async def refused_keeping_typing(
    request: Request,
    state: ApplicationState,
    error: Exception,
    said: Sequence[str] = (),
    status: int | None = None,
) -> Response:
    """A store's refusal of a press, as its page says it: for a review that expired, or that the
    sweep has since removed, with the press's typing given back, after what the press did if
    anything of it committed; any other as it always is."""
    if isinstance(error, DraftExpired):
        return await gone_with_typing(request, state, "expired", said, status or 410)
    if isinstance(error, DraftNotFound):
        return await gone_with_typing(request, state, "absent", said, status or 404)
    return store_refusal(request, state, error)


async def gone_now(state: ApplicationState, draft_id: str) -> str | None:
    """Whether the review, read now, has expired (``expired``) or is gone
    (``absent``); None while it can still be read, open or closed."""
    student = state.project_state.student_id()
    try:
        await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except DraftExpired:
        return "expired"
    except DraftNotFound:
        return "absent"
    except STORE_REFUSALS:
        return None
    return None


async def snapshot_keeping_typing(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    said: Sequence[str] = (),
    status: int | None = None,
    done: Sequence[str] = (),
) -> DraftSnapshot | Response:
    """The review as one read sees it after a press, or the page that says why it can't be
    shown: for a review that expired or is gone, what the press did and its typing. After a
    save that committed (``done``, what it saved), a review the store refuses to read answers
    with what it saved and the press's typing (``saved_unread``), never that nothing was
    saved."""
    student = state.project_state.student_id()
    try:
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except (DraftExpired, DraftNotFound) as error:
        return await refused_keeping_typing(request, state, error, [*done, *said], status)
    except STORE_REFUSALS as error:
        if done:
            return await saved_unread(request, state, done, said)
        return await refused_keeping_typing(request, state, error, said, status)
    if snapshot.closed_at is not None:
        return said_page(request, marks_of(state), SAID["closed"], 200)
    return snapshot


STORE_REFUSALS: Final = (
    TooManyImages,
    DraftClosed,
    DraftExpired,
    DraftFrozen,
    DraftNotFound,
    StoreBusy,
    WriterBusy,
)


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
    student = state.project_state.student_id()
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
        return store_refusal(request, state, error)
    if added is None:
        lines = [image_refusal(place + 1, kind) for place, kind in prepared.refused]
        return said_page(request, marks_of(state), "Nothing was added.", 422, *lines)
    places = [place for place, _ in prepared.images]
    told = await upload_told(state, added)
    return to_review(state, added.draft_id, upload_said(added, places, prepared.refused), told)


async def upload_told(state: ApplicationState, added: Added) -> list[str]:
    """What an upload says it added, beside how many screenshots the review holds and how many
    are read, as the review stands after its commit."""
    if not added.added:
        return []
    student = state.project_state.student_id()
    try:
        after = await asyncio.to_thread(state.import_drafts.snapshot, student, added.draft_id)
    except STORE_REFUSALS:
        return []
    read = sum(1 for image in after.images if image.read)
    return uploaded_said(len(added.added), len(after.images), read)


@router.post("/{draft_id}/images")
async def more_images(request: Request, draft_id: str, state: State) -> Response:
    """Add an upload's images to an open review, with no version comparison; the review is
    checked before any byte of the body is read, and again when the images are stored."""
    if not opaque(draft_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    student = state.project_state.student_id()
    try:
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
        if snapshot.closed_at is not None or snapshot.finished:
            raise DraftClosed
        if snapshot.frozen:
            raise DraftFrozen
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
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
        return store_refusal(request, state, error)
    places = [place for place, _ in prepared.images]
    told = await upload_told(state, added)
    return to_review(state, draft_id, upload_said(added, places, prepared.refused), told)


async def tables_of(state: ApplicationState, draft_id: str) -> DraftForSave | None:
    """A homework review's tables as the save reads them; None for any other review."""
    record = state.project_state
    return await asyncio.to_thread(record.draft_for_save, record.student_id(), draft_id)


async def change_lines(
    state: ApplicationState, draft_id: str, before: DraftForSave | None
) -> list[str]:
    """What the page after a change of screenshots says of the answers it cleared and the rows
    left unsaved it kept or asks about again, from the review's tables before the change and
    its readings after; nothing when the review isn't open homework on both sides."""
    if before is None or before.closed_at is not None:
        return []
    after = await tables_of(state, draft_id)
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
    student = state.project_state.student_id()
    before = await tables_of(state, draft_id)
    try:
        await asyncio.to_thread(state.import_drafts.remove_image, student, draft_id, image_id)
    except UnknownImage:
        return to_review(state, draft_id, [SAID["image_gone"]])
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
    return to_review(state, draft_id, await change_lines(state, draft_id, before))


@router.get("/{draft_id}/images/{image_id}")
async def view_image(request: Request, draft_id: str, image_id: str, state: State) -> Response:
    """The prepared PNG of one image: the bytes stored, sent to the reader and shown."""
    if not opaque(draft_id) or not opaque(image_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    student = state.project_state.student_id()
    try:
        png = await asyncio.to_thread(state.import_drafts.image_png, student, draft_id, image_id)
    except UnknownImage:
        return said_page(request, marks_of(state), SAID["image_gone"], 404)
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
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
    state: ApplicationState, snapshot: DraftSnapshot
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
        identity = state.project_state.identity_of(key, line)
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


async def state_of_review(
    state: ApplicationState, snapshot: DraftSnapshot, corrections: Corrections
) -> ReviewState | None:
    """The review's state as the save works it out, from its stored tables and the record now;
    None when there is no such homework review."""
    record = state.project_state
    tables = await asyncio.to_thread(record.draft_for_save, record.student_id(), snapshot.draft_id)
    if tables is None:
        return None
    return await asyncio.to_thread(
        review_state, record, tables, now=state.real_clock.now(), corrections=corrections
    )


async def rows_of(
    request: Request,
    state: ApplicationState,
    snapshot: DraftSnapshot,
    said: list[str],
    typed: Corrections | None = None,
    ticked: Sequence[str] | None = None,
    both: Sequence[str] = (),
) -> RowsShown | None:
    """The review's rows once anything is read, worked out from its stored state and the record
    as the save works them out, with the corrections and the groups ticked as saved for later
    or, when a press's typing comes back, as it sent them; None before anything is read."""
    if not any(r.kind == "homework_list" for r in snapshot.readings):
        return None
    kept = await saved_corrections(state, snapshot)
    corrections = kept if typed is None else typed
    review = await state_of_review(state, snapshot, corrections)
    open_keys = [row.key for row in snapshot.rows if row.state == "open"]
    items_open = any(not row.readable and row.state == "open" for row in snapshot.rows)
    if review is None or (not review.open and not review.asking and not items_open):
        return None
    unread = homework_assembly(snapshot.readings, now=state.real_clock.now()).unread
    named = await image_names(request, state, snapshot)
    if named is None:
        said.append(SAID["no_name_check"])
    return rows_shown(
        review,
        places={image.sha256: index + 1 for index, image in enumerate(snapshot.images)},
        images=[] if named is None else named[1],
        page_key=snapshot.page_key,
        version=snapshot.version,
        progress_revision=snapshot.progress_revision,
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
) -> Response:
    """The review: what a press did and found wrong, each in its own style, its images with
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
    request: Request, state: ApplicationState, draft_id: str
) -> DraftSnapshot | Response:
    """The review as one read sees it, or the page that says why it can't be shown."""
    if not opaque(draft_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    student = state.project_state.student_id()
    try:
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
    if snapshot.closed_at is not None:
        return said_page(request, marks_of(state), SAID["closed"], 200)
    return snapshot


@router.get("/{draft_id}")
async def review(request: Request, draft_id: str, state: State) -> Response:
    """The review: its images, how each stands, and the Read press for those not read."""
    found = await snapshot_or_page(request, state, draft_id)
    if isinstance(found, Response):
        return found
    note = note_read(state.result_key, draft_id, request.cookies.get(NOTE_COOKIE))
    said = note.said
    if said or note.told or note.done:
        answering(request)
    rows = await rows_of(request, state, found, said)
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
    student = state.project_state.student_id()
    before = await tables_of(state, draft_id)
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
    after = await snapshot_or_page(request, state, draft_id)
    if isinstance(after, Response):
        return after
    said = await change_lines(state, draft_id, before)
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
    rows = await rows_of(request, state, after, said)
    return review_page(request, state, after, said, rows=rows, told=told)


def read_said(snapshot: DraftSnapshot, now: datetime) -> list[str]:
    """What a Read press says the review's screenshots read as: how many are read as homework
    lists and the assignments they hold, and how many are read as something else."""
    kinds = {reading.sha256: reading.kind for reading in snapshot.readings}
    read = [image for image in snapshot.images if image.read]
    lists = sum(1 for image in read if kinds.get(image.sha256) == "homework_list")
    out = []
    if lists:
        out.append(read_said_line(lists, len(homework_assembly(snapshot.readings, now=now).rows)))
    if len(read) > lists:
        out.append(not_a_list_line(len(read) - lists))
    return out


def read_said_line(lists: int, found: int) -> str:
    """ "1 screenshot read: 3 assignments found."."""
    return (
        f"{lists} screenshot{'' if lists == 1 else 's'} read: "
        f"{found} assignment{'' if found == 1 else 's'} found."
    )


def not_a_list_line(count: int) -> str:
    """ "1 screenshot read: not a homework list."."""
    return f"{count} screenshot{'' if count == 1 else 's'} read: not a homework list."


# ------------------------------------------------------------------ the review's presses

CHANGED_IN_ANOTHER_TAB: Final = "This review changed in another tab. Check the rows again."
PRESS_SAID: Final = {
    "kept": (
        "Your answers are kept. Corrections you typed aren't; use Save review for later to "
        "keep those."
    ),
    "keep_stale": "Your answers on that page weren't kept.",
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
    "save_stale": "Your answers on that page weren't kept, and nothing was saved.",
    "save_invalid": (
        "Some of these questions changed before you answered, so nothing was kept or saved. "
        "Answer them again."
    ),
    "save_failed": "Your answers couldn't be kept, and nothing was saved. Try again.",
    "later": "Your answers are kept, and your corrections are saved for later.",
    "later_behind": (
        "Another tab saved this review for later since this page opened. Your typing is still "
        "here; nothing was saved."
    ),
    "later_stale": "Nothing was kept.",
    "later_invalid": (
        "Some of these questions changed before you answered, so nothing was kept or saved for "
        "later. Answer them again."
    ),
    "later_failed": "Your answers couldn't be kept, and nothing was saved for later. Try again.",
    "answers_kept": "Your answers are kept.",
    "none_chosen": "Choose at least one assignment to save.",
    "left_open": "Your answers on the rows left open are kept.",
    "already": "Already saved from this review.",
    "changed": (
        "Saving an assignment you ticked would now do this instead. Check it and save again."
    ),
    "changed_by_answers": (
        "Your answers are kept. They change what saving does, so nothing was saved yet. Check "
        "the effects below, then save."
    ),
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
    "stale": "this review changed in another tab",
}
"""Why a press's homework wasn't saved, for "The homework wasn't saved: {reason}."."""


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
    box that can't be left as posted, in the box's own words, and that nothing was kept or
    saved, once."""
    boxes = [NEEDED[field] for _, field in post.needed if field in NEEDED]
    boxes += [PAGE_SAID["both"]] if post.both else []
    if post.too_long:
        return [PRESS_SAID["too_long"], *dict.fromkeys(boxes)]
    return [*dict.fromkeys(boxes), PRESS_SAID["nothing_kept"]]


def not_saved(outcome: NotSaved, answered: bool) -> list[str]:
    """What a press says when the homework transaction didn't commit: its answers kept when the
    press stored any, and why nothing of the homework was written."""
    kept = [PRESS_SAID["answers_kept"]] if answered else []
    if outcome.reason == "changed":
        return [PRESS_SAID["changed_by_answers"] if answered else PRESS_SAID["changed"]]
    if outcome.reason in ("busy", "failed"):
        return [*kept, PRESS_SAID[outcome.reason]]
    because = NOT_SAVED_BECAUSE.get(outcome.reason, NOT_SAVED_BECAUSE["stale"])
    return [*kept, f"The homework wasn't saved: {because}. Nothing of it was written."]


async def review_post(
    request: Request, state: ApplicationState, found: DraftSnapshot
) -> tuple[ReviewPost | None, str]:
    """The post read against what its page signed, or None when it doesn't match or the review
    has no homework to read it against, with the press it named."""
    form = await form_of(request)
    items = [(name, value) for name, value in form.multi_items() if isinstance(value, str)]
    named = next((value for name, value in items if name == "press"), "keep")
    saved = await saved_corrections(state, found)
    review = await state_of_review(state, found, saved)
    if review is None:
        return None, named
    try:
        if any(name == "offer" for name, _ in items):
            return read_change(items, found.page_key, review, saved), "keep"
        return read_review(items, found.page_key, review), named
    except PostRefused:
        return None, named


@router.post("/{draft_id}/review")
async def review_press(request: Request, draft_id: str, state: State) -> Response:
    """The review form's three presses, and an offer or a found row posted whole: Keep these
    answers, Save selected homework and Save review for later. A press with a correction
    longer than its field takes keeps and saves nothing, and answers 422 with the review and
    its typing, that correction whole in its box."""
    if not opaque(draft_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    student = state.project_state.student_id()
    try:
        found = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except STORE_REFUSALS as error:
        return await replayed(request, state, draft_id) or await refused_keeping_typing(
            request, state, error
        )
    if found.closed_at is not None:
        closed = said_page(request, marks_of(state), SAID["closed"], 200)
        return await replayed(request, state, draft_id) or closed
    post, named = await review_post(request, state, found)
    if post is None:
        refused = {"save": "save_invalid", "later": "later_invalid"}.get(named, "keep_invalid")
        return to_review(state, draft_id, [PRESS_SAID[refused]])
    if post.refused:
        return await with_typing(request, state, draft_id, post, refused_said(post), 422)
    if post.press == "later":
        return await later_press(request, state, found, post)
    if post.press == "save":
        return await save_press(request, state, found, post)
    student = state.project_state.student_id()
    try:
        await asyncio.to_thread(
            state.import_drafts.keep_answers,
            student,
            draft_id,
            version=post.version,
            answers=list(post.answers),
            withdrawn=list(post.withdrawn),
        )
    except ReviewChanged:
        said = [CHANGED_IN_ANOTHER_TAB, PRESS_SAID["keep_stale"]]
        return await with_typing(request, state, draft_id, post, said, 409)
    except (StoreBusy, WriterBusy):
        return await with_typing(request, state, draft_id, post, [PRESS_SAID["keep_failed"]], 503)
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return await refused_keeping_typing(request, state, error)
    return await with_typing(request, state, draft_id, post, [PRESS_SAID["kept"]], 200)


async def with_typing(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    post: ReviewPost,
    said: Sequence[str],
    status: int,
    done: Sequence[str] = (),
) -> Response:
    """A press that saved nothing, or a save that left the review open, answers with the
    review as it stands now, the corrections it typed and the groups it ticked still in the
    form, so nothing typed is lost and an answer just kept stays matched to the corrections it
    was given with. A press that posts no ticks, an offer or a found row, is ticked as a plain
    load would tick the page. A review that expired meanwhile answers with what the press did
    and its typing to copy."""
    after = await snapshot_keeping_typing(request, state, draft_id, said, status, done)
    if isinstance(after, Response):
        return after
    lines = list(said)
    ticked = post.units if post.ticked else None
    rows = await rows_of(
        request, state, after, lines, typed=post.corrections, ticked=ticked, both=post.both
    )
    return review_page(request, state, after, lines, status, rows=rows, done=done)


async def later_press(
    request: Request, state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> Response:
    """Save review for later: the answers and the typed corrections, in one transaction, the
    corrections only when no other tab saved for later since the page."""
    student = state.project_state.student_id()
    review = await state_of_review(state, found, post.corrections)
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
        said = [CHANGED_IN_ANOTHER_TAB, PRESS_SAID["later_stale"]]
        return await with_typing(request, state, found.draft_id, post, said, 409)
    except (StoreBusy, WriterBusy):
        said = [PRESS_SAID["later_failed"]]
        return await with_typing(request, state, found.draft_id, post, said, 503)
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return await refused_keeping_typing(request, state, error)
    if done.progress_saved:
        later = [PRESS_SAID["later"], saved_for_later_at(state)]
        gone = await gone_now(state, found.draft_id)
        if gone is not None:
            return await gone_with_typing(request, state, gone, later, 200)
        return to_review(state, found.draft_id, later)
    said = [PRESS_SAID["answers_kept"], PRESS_SAID["later_behind"]]
    return await with_typing(request, state, found.draft_id, post, said, 409)


async def save_press(
    request: Request, state: ApplicationState, found: DraftSnapshot, post: ReviewPost
) -> Response:
    """Save selected homework: a press whose acceptance id is recorded shows that save's outcome;
    otherwise the answers kept at the page's version, then the homework transaction for the
    groups ticked, and closing the review when that finished it."""
    record = state.project_state
    student = record.student_id()
    draft_id = found.draft_id
    recorded = await asyncio.to_thread(
        record.homework_acceptance, student, draft_id, post.acceptance
    )
    if recorded is not None:
        return await saved_page(request, state, draft_id, post.acceptance, [], post)
    try:
        kept = await asyncio.to_thread(
            state.import_drafts.keep_answers,
            student,
            draft_id,
            version=post.version,
            answers=list(post.answers),
            withdrawn=list(post.withdrawn),
        )
    except ReviewChanged:
        said = [CHANGED_IN_ANOTHER_TAB, PRESS_SAID["save_stale"]]
        return await with_typing(request, state, draft_id, post, said, 409)
    except (StoreBusy, WriterBusy):
        return await with_typing(request, state, draft_id, post, [PRESS_SAID["save_failed"]], 503)
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return await refused_keeping_typing(request, state, error)
    answered = kept.stored > 0 or kept.withdrawn > 0
    kept_said = [PRESS_SAID["answers_kept"]] if answered else []
    if not post.units:
        said = [*kept_said, PRESS_SAID["none_chosen"]]
        return await with_typing(request, state, draft_id, post, said, 200)
    named = await image_names(request, state, found)
    if named is None:
        said = [*kept_said, SAID["no_name_check"]]
        return await with_typing(request, state, draft_id, post, said, 500)
    press = SavePress(
        student,
        draft_id,
        kept.version,
        post.acceptance,
        post.units,
        post.digests,
        post.defaults,
        post.corrections,
        {image.sha256: (image.status, image.form) for image in named[1]},
        named[0],
        role_of(state),
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
    return await outcome_page(request, state, draft_id, outcome, answered, post)


async def outcome_page(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    outcome: SaveOutcome,
    answered: bool,
    post: ReviewPost,
) -> Response:
    """What a Save press answers with, by what committed: "nothing was saved" only when the
    homework transaction didn't commit, and a save that committed is reported as saved, in the
    success pattern, with anything after it that didn't finish. A review that expired, or is
    gone, gives the press's typing back with what it says, never in place of a save that
    committed."""
    if isinstance(outcome, NotSaved):
        if outcome.reason in ("absent", "expired"):
            return await gone_with_typing(request, state, outcome.reason, [], 409)
        if outcome.reason == "closed":
            return said_page(request, marks_of(state), SAID[outcome.reason], 409)
        status = 503 if outcome.reason in ("busy", "failed") else 409
        return await with_typing(
            request, state, draft_id, post, not_saved(outcome, answered), status
        )
    if isinstance(outcome, AlreadySaved):
        return await with_typing(request, state, draft_id, post, [PRESS_SAID["already"]], 200)
    if isinstance(outcome, Replayed):
        return await saved_page(request, state, draft_id, outcome.acceptance_id, [], post)
    done = [counted(*outcome.counts)]
    if outcome.already_units:
        done.append(PRESS_SAID["already"])
    problems = [PRESS_SAID["changed"]] if outcome.changed_units else []
    if outcome.incomplete:
        lines = [PRESS_SAID["incomplete"], *done, *problems]
        gone = await gone_now(state, draft_id)
        if gone is not None:
            return await gone_with_typing(request, state, gone, lines, 200)
        return said_page(request, marks_of(state), lines[0], 200, *lines[1:])
    if outcome.closed:
        week = await week_of(state, outcome.pairs)
        return saved_outcome(request, state, [*done, *problems], week)
    if answered:
        done.append(PRESS_SAID["left_open"])
    return await partly_saved(request, state, draft_id, post, done, problems)


async def week_of(state: ApplicationState, pairs: Iterable[tuple[str, str]]) -> str | None:
    """Her week at the earliest due date among the assignments a save wrote, with that card in
    view; None when none of them has a due date, or when the assignments can't be read once the
    save has committed, so the saved outcome still shows, with no way to her week."""
    targets = {target for _, target in pairs}
    try:
        everything = await asyncio.to_thread(state.project_state.all_assignments)
    except Exception as error:
        logger.warning("Her week couldn't be read after a save: %s", type(error).__name__)
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
) -> Response:
    """A save that finished the review, as the grade save's outcome shows one: the steps line
    at Saved, the counts as the panel's heading, her week, and that the review is finished.
    Where whether it finished isn't known (``finished`` false), it isn't said, what else the
    press said (``notes``) is, and the corrections the post typed (``typed``) are given back to
    copy."""
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {
            "said": [SAID["closed"], *notes] if finished else list(notes),
            "outcome": list(lines),
            "week": week,
            "typed": list(typed),
            "typed_said": TYPED_SAID,
            "marks": marks_of(state),
            "page": "parent",
        },
        status_code=200,
    )


async def partly_saved(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    post: ReviewPost,
    done: list[str],
    said: Sequence[str] = (),
) -> Response:
    """A save that leaves the review open: the outcome in the success style, and the rows left
    open with the corrections the press typed still in the form, marked as not saved, none of
    them stored. A review that expired since answers with the outcome and the typing to
    copy."""
    after = await snapshot_keeping_typing(request, state, draft_id, said, 200, done)
    if isinstance(after, Response):
        return after
    problems = list(said)
    if unsaved_typing(after, post.corrections, await saved_corrections(state, after)):
        problems.append(PRESS_SAID["unsaved_typing"])
    return await with_typing(request, state, draft_id, post, problems, 200, done)


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


async def replayed(request: Request, state: ApplicationState, draft_id: str) -> Response | None:
    """A Save press resent after its review closed: the outcome its acceptance id recorded for
    this review, when it recorded one; else None, so an id recorded for another review gets
    this one's own answer."""
    if not opaque(draft_id):
        return None
    form = await form_of(request)
    acceptance = next((v for n, v in form.multi_items() if n == "acceptance"), "")
    if not isinstance(acceptance, str) or not opaque(acceptance):
        return None
    record = state.project_state
    student = record.student_id()
    if await asyncio.to_thread(record.homework_acceptance, student, draft_id, acceptance) is None:
        return None
    return await saved_page(request, state, draft_id, acceptance, [])


async def saved_page(
    request: Request,
    state: ApplicationState,
    draft_id: str,
    acceptance: str,
    said: list[str],
    post: ReviewPost | None = None,
) -> Response:
    """A resent press whose acceptance id is recorded: that save's counts, and nothing
    written. A review it finished says so. With the review still open, the press gets its
    typing back in the review, as the save it repeats did, its post read here when the
    caller had none. A review the store refuses to read (busy, or gone) can't say whether it
    finished: the counts show in the success panel with no word of that, and every
    correction the post typed is given back to copy."""
    record = state.project_state
    student = record.student_id()
    counts = await asyncio.to_thread(record.homework_counts, student, draft_id, acceptance)
    lines = [*said, counted(*counts)] if counts else said
    try:
        snapshot = await asyncio.to_thread(state.import_drafts.snapshot, student, draft_id)
    except STORE_REFUSALS:
        snapshot = None
    if snapshot is None or snapshot.closed_at is not None or snapshot.finished:
        if not counts:
            return said_page(request, marks_of(state), lines[0], 200, *lines[1:])
        pairs = await asyncio.to_thread(record.homework_acceptance, student, draft_id, acceptance)
        week = await week_of(state, pairs or ())
        if snapshot is None:
            return await saved_unread(request, state, lines, week=week)
        return saved_outcome(request, state, lines, week)
    if post is None:
        post, _ = await review_post(request, state, snapshot)
    if post is not None:
        return await partly_saved(request, state, draft_id, post, lines)
    return to_review(state, draft_id, done=lines)


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
    posted, whole = await fields_of(
        request, frozenset({"piece", "words"}), may_be_absent=frozenset({"words"})
    )
    if not whole:
        return said_page(request, marks_of(state), FIND_SAID["gone"], 400)
    piece_name = posted.get("piece", "")
    words = posted.get("words", "")[:MOST_WORDS]
    review = await state_of_review(state, found, await saved_corrections(state, found))
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
) -> tuple[int, list[str]] | None:
    """A small form's version and keys, when its fields are whole and signed with the review's
    page key; None when they aren't."""
    fields, whole = await fields_of(request, SMALL_FIELDS[action])
    if not whole or not fields.get("version", "").isdigit() or len(fields["version"]) > 12:
        return None
    signed = [("version", fields["version"])]
    if "keys" in SMALL_FIELDS[action]:
        signed.append(("keys", fields["keys"]))
    if action != "set-aside":
        signed.append(("action", action))
    if not same_signature(fields["check"], page_check(found.page_key, signed)):
        return None
    try:
        keys = json.loads(fields.get("keys", "[]"))
    except ValueError:
        return None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        return None
    return int(fields["version"]), keys


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
    version, keys = posted
    student = state.project_state.student_id()
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
        return to_review(state, draft_id, [CHANGED_IN_ANOTHER_TAB, AFTER_SAID["nothing_set_aside"]])
    except (StoreBusy, WriterBusy):
        return to_review(state, draft_id, [AFTER_SAID["busy"]])
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return store_refusal(request, state, error)
    first = [AFTER_SAID["set_aside"]] if action == "set-aside" else []
    if done.closed:
        lines = finished_said(done, *first)
        return said_page(request, marks_of(state), lines[0], 200, *lines[1:])
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
    student = state.project_state.student_id()
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
        return to_review(state, draft_id, [CHANGED_IN_ANOTHER_TAB, AFTER_SAID["nothing_discarded"]])
    except (StoreBusy, WriterBusy):
        return to_review(state, draft_id, [AFTER_SAID["busy"]])
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return store_refusal(request, state, error)
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
