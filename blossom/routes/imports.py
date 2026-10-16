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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from blossom.anthropic_client import model_configured
from blossom.dependencies import ApplicationState, get_application_state
from blossom.routes.forms import fields_of
from blossom.routes.grades import from_this_computer
from blossom.screenshot_reading import read_screenshots
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.import_drafts import (
    DRAFT_LIFETIME,
    MOST_IMAGES_PRESENT,
    TOKEN_SHAPE,
    Added,
    AttemptUnderWay,
    DraftClosed,
    DraftExpired,
    DraftNotFound,
    DraftSnapshot,
    TooManyImages,
    UnknownImage,
)
from blossom.templating import page_templates
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
    "unread": "Not read yet. Read again.",
    "ended": "Not read in time. Read again.",
    "read": "Read.",
    "wrong_kind": "This doesn't look like the school's homework list.",
}
"""How an image stands, as its review says it, all in one place: an image no attempt covers,
one whose attempt ended with no result kept, one read, and one read as something else."""

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
    "unchecked": (
        "Nothing was read: this page's Read button couldn't be checked. Press Read again."
    ),
    "no_key": "Screenshots can't be read here: Blossom has no API key.",
    "only_here": (
        "Screenshots can be added only on the computer running Blossom, or with sign-in on."
    ),
    "read_busy": "Blossom was busy, so nothing was read. Press Read again.",
}
LIMITS: Final = (
    "Up to 10 screenshots at a time, PNG or JPEG, up to 12 MB each. A review holds up to 10 "
    "screenshots."
)
DISCLOSED: Final = (
    "These screenshots will be sent to Anthropic's API to be read. Nothing is sent until you "
    "press Read."
)

templates = page_templates()


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
    """The import routes' dependency: with sign-in off, only this computer."""
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
    """A page that says why nothing more happened, its first sentence as the alert."""
    return templates.TemplateResponse(
        request,
        "import_said.html",
        {"said": [said, *more], "marks": marks, "page": "parent"},
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


def note_for(key: bytes, draft_id: str, said: Sequence[str]) -> str:
    """A note for one review: what an upload refused or found there already, signed."""
    body = base64.urlsafe_b64encode(json.dumps([draft_id, list(said)]).encode()).decode()
    return f"{body}.{note_check(key, body)}"


def note_read(key: bytes, draft_id: str, value: str | None) -> list[str]:
    """The sentences a note holds for this review, or none when it is another's or unsigned."""
    if not value or "." not in value:
        return []
    body, check = value.rsplit(".", 1)
    if not hmac.compare_digest(check, note_check(key, body)):
        return []
    try:
        named, said = json.loads(base64.urlsafe_b64decode(body.encode()))
    except (ValueError, TypeError):
        return []
    if named != draft_id or not isinstance(said, list):
        return []
    return [str(line) for line in said if isinstance(line, str)]


def to_review(state: ApplicationState, draft_id: str, said: Sequence[str] = ()) -> Response:
    """The redirect every committed press answers with, carrying its note when it has one."""
    answer = RedirectResponse(f"{PREFIX}/{draft_id}", status_code=303)
    if said:
        answer.set_cookie(
            NOTE_COOKIE,
            note_for(state.result_key, draft_id, said),
            max_age=NOTE_SECONDS,
            path=f"{PREFIX}/{draft_id}",
            httponly=True,
            samesite="strict",
        )
    return answer


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
    if isinstance(error, StoreBusy | WriterBusy):
        return said_page(request, marks, SAID["busy"], 503)
    return said_page(request, marks, SAID["absent"], 404)


STORE_REFUSALS: Final = (
    TooManyImages,
    DraftClosed,
    DraftExpired,
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
    return to_review(state, added.draft_id, upload_said(added, places, prepared.refused))


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
    return to_review(state, draft_id, upload_said(added, places, prepared.refused))


@router.post("/{draft_id}/images/{image_id}/remove")
async def remove_image(request: Request, draft_id: str, image_id: str, state: State) -> Response:
    """Remove one image and its reading, with no version comparison; nothing of a body is read."""
    if not opaque(draft_id) or not opaque(image_id):
        return said_page(request, marks_of(state), SAID["absent"], 404)
    student = state.project_state.student_id()
    try:
        await asyncio.to_thread(state.import_drafts.remove_image, student, draft_id, image_id)
    except UnknownImage:
        return to_review(state, draft_id, [SAID["image_gone"]])
    except STORE_REFUSALS as error:
        return store_refusal(request, state, error)
    return to_review(state, draft_id)


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


def review_page(
    request: Request,
    state: ApplicationState,
    snapshot: DraftSnapshot,
    said: Sequence[str] = (),
    status: int = 200,
) -> Response:
    """The review: its images with their labels, the Read press for the unread ones when a key
    is set and no attempt is current, and the form that adds more."""
    keyed = model_configured(state.settings)
    unread = [image.image_id for image in snapshot.images if not image.read]
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
                {"id": image.image_id, "place": index + 1, "label": label_of(snapshot, index)}
                for index, image in enumerate(snapshot.images)
            ],
            "said": list(said),
            "under_way": None if when is None else under_way(when),
            "read_ids": ",".join(unread) if keyed and unread and when is None else None,
            "read_check": read_check(snapshot.page_key, unread),
            "no_key": None if keyed else SAID["no_key"],
            "disclosed": DISCLOSED,
            "limits": LIMITS,
            "full": len(snapshot.images) >= MOST_IMAGES_PRESENT,
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
    said = note_read(state.result_key, draft_id, request.cookies.get(NOTE_COOKIE))
    answer = review_page(request, state, found, said)
    if request.cookies.get(NOTE_COOKIE) is not None:
        answer.delete_cookie(NOTE_COOKIE, path=f"{PREFIX}/{draft_id}")
    return answer


@router.post("/{draft_id}/read")
async def read(request: Request, draft_id: str, state: State) -> Response:
    """A Read press over the unread images its page showed, signed with the review's key; it
    returns by the attempt's deadline with the review as it stands then."""
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
        or not hmac.compare_digest(fields.get("check", ""), read_check(found.page_key, named))
    ):
        return review_page(request, state, found, [SAID["unchecked"]], 400)
    student = state.project_state.student_id()
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
    except (DraftClosed, DraftExpired, DraftNotFound) as error:
        return store_refusal(request, state, error)
    after = await snapshot_or_page(request, state, draft_id)
    if isinstance(after, Response):
        return after
    said: list[str] = []
    if outcome is not None:
        places = {image.image_id: index + 1 for index, image in enumerate(after.images)}
        read_now = {image.image_id for image in after.images if image.read}
        said = [
            busy_reading(places[image_id])
            for image_id in outcome.busy
            if image_id in places and image_id not in read_now
        ]
    return review_page(request, state, after, said)
