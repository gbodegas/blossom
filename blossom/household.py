"""Who is at the keyboard: a sign-in for a household whose pages are reached from three devices.

Her computer, her tablet, and a parent's computer all open Blossom over the
home network, so the pages cannot stay open to whoever reaches the server.
Two passphrases, one hers and one a parent's, name the two people the system
serves. Typing hers opens her week; typing a parent's opens both pages. There
are no accounts and no names: the passphrase is the whole identity, which is
as much as a family of three needs and as little as can be kept.

A sign-in is remembered in a cookie the server signs with a secret kept beside
the household's database, so a restart keeps everyone signed in and a cookie
made up elsewhere is refused. The signature is a keyed hash from the standard
library; nothing here needs a library, a token service, or the internet. The
cookie is not marked secure, because the home network carries plain HTTP; the
guide says never to expose the server beyond it.

With neither passphrase set the gate stands open, which is the right shape
for the tests, the sample, and a machine only the family touches.
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from blossom.principals import Principal
from blossom.settings import Settings
from blossom.stores.checkpoints import refuse_unsafe_path
from blossom.templating import page_templates

COOKIE: Final = "blossom_household"
SESSION_SECONDS: Final = 30 * 24 * 60 * 60
"""A sign-in lasts a month, then asks again."""
SECRET_NAME: Final = "household.secret"  # noqa: S105  (a file name, not a secret)
OPEN_PREFIXES: Final = ("/sign-in", "/sign-out", "/static/")
"""What anyone may reach: the way in, the way out, and the stylesheet the way in needs."""

templates = page_templates()


def secret_beside(state_path: Path) -> bytes:
    """The signing secret kept beside the household's database, made once and reused.

    The file goes under the same guard as the database: not on a share, not
    in a synced folder. A new secret would sign everyone out, so it is made
    only when none exists.
    """
    path = refuse_unsafe_path(state_path).with_name(SECRET_NAME)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip().encode("ascii")


def role_for(passphrase: str, settings: Settings) -> Principal | None:
    """Which person a passphrase names, or ``None``. Both comparisons run every time."""
    given = passphrase.encode("utf-8")
    student = settings.student_passphrase or ""
    parent = settings.parent_passphrase or ""
    is_student = secrets.compare_digest(given, student.encode("utf-8"))
    is_parent = secrets.compare_digest(given, parent.encode("utf-8"))
    if not settings.household_sign_in:
        return None
    if is_student:
        return Principal.STUDENT
    if is_parent:
        return Principal.PARENT
    return None


def issue(role: Principal, secret: bytes, now: datetime) -> str:
    """A signed token naming who signed in and when."""
    payload = f"{role.value}:{int(now.timestamp())}"
    signature = hmac.new(secret, payload.encode("ascii"), "sha256").hexdigest()
    return f"{payload}:{signature}"


def read_token(token: str | None, secret: bytes, now: datetime) -> Principal | None:
    """Who a token names, or ``None`` for one that is missing, altered, aged out, or odd."""
    if not token:
        return None
    parts = token.split(":")
    if len(parts) != 3:
        return None
    role, issued, signature = parts
    payload = f"{role}:{issued}"
    expected = hmac.new(secret, payload.encode("ascii"), "sha256").hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    if not issued.isdigit() or int(now.timestamp()) - int(issued) > SESSION_SECONDS:
        return None
    if role not in (Principal.STUDENT.value, Principal.PARENT.value):
        return None
    return Principal(role)


def home_of(role: Principal) -> str:
    """Where each person lands after signing in."""
    return "/parent" if role is Principal.PARENT else "/student/due-this-week"


def may_open(role: Principal, path: str) -> bool:
    """Her page is hers and a parent's; the parent's page and the verifier's are a parent's."""
    if path.startswith("/student"):
        return True
    return role is Principal.PARENT


def wants_a_page(request: Request) -> bool:
    """Whether the request comes from a browser looking for a page rather than JSON."""
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


class HouseholdGate(BaseHTTPMiddleware):
    """Ask who is there before any page or route answers, when the sign-in is on.

    A browser without a sign-in is sent to the sign-in page and back again
    afterward; a JSON call is answered 401. A signed-in student asking for a
    parent's page is told the page is not hers, 403, and offered her own.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self.settings.household_sign_in:
            return await call_next(request)
        path = request.url.path
        secret: bytes = request.app.state.household_secret
        role = read_token(request.cookies.get(COOKIE), secret, datetime.now(UTC))
        request.state.household = role
        if path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        if role is None:
            if wants_a_page(request):
                # The whole address asked for, query included, so the sign-in
                # brings the browser back to the page it wanted.
                wanted = path if not request.url.query else f"{path}?{request.url.query}"
                return RedirectResponse(f"/sign-in?next={quote(wanted, safe='')}", status_code=303)
            return JSONResponse({"detail": "Sign in first."}, status_code=401)
        if not may_open(role, path):
            if wants_a_page(request):
                return templates.TemplateResponse(
                    request, "not_for_you.html", {"home": home_of(role)}, status_code=403
                )
            return JSONResponse({"detail": "This page is for a parent."}, status_code=403)
        return await call_next(request)
