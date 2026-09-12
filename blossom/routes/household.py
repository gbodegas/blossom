"""The way in and the way out: sign-in with a passphrase, sign-out by a press."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.dependencies import ApplicationState, get_application_state
from blossom.household import COOKIE, SESSION_SECONDS, home_of, issue, role_for
from blossom.principals import Principal
from blossom.templating import page_templates

router = APIRouter(tags=["household"])
templates = page_templates()
State = Annotated[ApplicationState, Depends(get_application_state)]

WRONG = "That passphrase is not one of ours. Try again."


def safe_next(value: str | None) -> str | None:
    """A place on this site to return to, or ``None`` for anything else."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None


def sign_in_page(
    request: Request, *, next_path: str | None, problem: str | None = None, status_code: int = 200
) -> HTMLResponse:
    """The page that asks who is there, with where to go afterward and what went wrong."""
    return templates.TemplateResponse(
        request,
        "sign_in.html",
        {"next": next_path, "problem": problem, "sign_in": True},
        status_code=status_code,
    )


@router.get("/sign-in", response_class=HTMLResponse, include_in_schema=False)
def sign_in(request: Request, state: State, next: str | None = None) -> Response:
    """Ask who is there. With the sign-in off, there is nothing to ask; her week is the answer."""
    if not state.settings.household_sign_in:
        return RedirectResponse(home_of(Principal.STUDENT), status_code=303)
    return sign_in_page(request, next_path=safe_next(next))


@router.post("/sign-in", response_class=HTMLResponse, include_in_schema=False)
def take_passphrase(
    request: Request,
    state: State,
    passphrase: Annotated[str, Form()] = "",
    next: Annotated[str | None, Form()] = None,
) -> Response:
    """Match the passphrase to a person and remember them for a month."""
    role = role_for(passphrase, state.settings)
    if role is None:
        return sign_in_page(
            request,
            next_path=safe_next(next),
            problem=WRONG,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    token = issue(role, request.app.state.household_secret, datetime.now(UTC))
    destination = safe_next(next) or home_of(role)
    response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax", path="/"
    )
    return response


@router.post("/sign-out", include_in_schema=False)
def sign_out() -> Response:
    """Forget who was here, on this device only."""
    response = RedirectResponse("/sign-in", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE, path="/")
    return response
