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

Each person's cookies are signed with a key drawn from that secret and their
own passphrase, so changing a passphrase in ``.env`` and restarting signs
that person's devices out and leaves the other's alone, and deleting the
secret file signs everyone out. "Sign out" forgets this device only; a copy
of its cookie taken before then works until the passphrase changes or the
month ends. Wrong passphrases from one device are counted, and after ten
the sign-in answers with a wait rather than keep guessing open.

With neither passphrase set the gate stands open, which is the right shape
for the tests, the sample, and a machine only the family touches.
"""

import hmac
import os
import secrets
import threading
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
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
SECRET_LENGTH: Final = 64
"""The secret on disk is 32 random bytes written as lowercase hex, and nothing else counts."""
HEX_DIGITS: Final = frozenset("0123456789abcdef")
OPEN_PREFIXES: Final = ("/sign-in", "/sign-out", "/static/")
"""What anyone may reach: the way in, the way out, and the stylesheet the way in needs."""
SKEW_SECONDS: Final = 5 * 60
"""How far ahead a token may be dated and still count: a clock a little fast when the
token was issued, not a clock a year ahead and corrected since."""
ATTEMPT_LIMIT: Final = 10
"""Wrong passphrases from one device before the sign-in asks it to wait."""
COOLDOWN_SECONDS: Final = 60
"""How long that device waits; its count starts over afterward."""
ATTEMPT_ADDRESSES: Final = 64
"""How many devices are counted at once; past that the oldest count is forgotten."""

templates = page_templates()


class UnreadableHouseholdSecret(ValueError):
    """The secret file is there but does not hold the whole secret written to it."""


def secret_beside(state_path: Path) -> bytes:
    """The signing secret kept beside the household's database, made once and reused.

    The file goes under the same guard as the database: not on a share, not
    in a synced folder. A new secret would sign everyone out, so it is made
    only when none exists, and it is put in place in one move, so a start cut
    short leaves no file rather than a short one. What is read back must be
    the whole secret and nothing more, read as written; a short or empty key
    is one a stranger could guess, and a changed file is a changed file, so
    anything else stops the start and names the file.
    """
    path = refuse_unsafe_path(state_path).with_name(SECRET_NAME)
    if not path.exists():
        make_secret(path)
    written = path.read_text(encoding="utf-8")
    if len(written) != SECRET_LENGTH or not HEX_DIGITS.issuperset(written):
        msg = (
            f"{path} does not hold a whole household secret; delete the file and start "
            "again, and everyone signs in once more"
        )
        raise UnreadableHouseholdSecret(msg)
    return written.encode("ascii")


def make_secret(path: Path) -> None:
    """Write a fresh secret beside ``path``, flush it to disk, and move it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{SECRET_NAME}.part")
    with partial.open("w", encoding="utf-8") as handle:
        handle.write(secrets.token_hex(SECRET_LENGTH // 2))
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)


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


def keys_for(secret: bytes, settings: Settings) -> dict[Principal, bytes]:
    """One signing key per person, drawn from the secret and that person's passphrase.

    The key is a keyed hash of the passphrase under the secret, so it says
    nothing about the passphrase and changes whenever the passphrase does: a
    changed passphrase signs that person's devices out at the next start and
    leaves the other person's alone, while unchanged settings give the same
    keys after a restart. With the sign-in off there are no keys.
    """
    keys: dict[Principal, bytes] = {}
    for role, passphrase in (
        (Principal.STUDENT, settings.student_passphrase),
        (Principal.PARENT, settings.parent_passphrase),
    ):
        if passphrase is not None:
            keys[role] = hmac.new(secret, passphrase.encode("utf-8"), "sha256").digest()
    return keys


def issue(role: Principal, key: bytes, now: datetime) -> str:
    """A token naming who signed in and when, signed with that person's key."""
    payload = f"{role.value}:{int(now.timestamp())}"
    signature = hmac.new(key, payload.encode("ascii"), "sha256").hexdigest()
    return f"{payload}:{signature}"


def read_token(
    token: str | None, keys: Mapping[Principal, bytes], now: datetime
) -> Principal | None:
    """Who a token names, or ``None`` for one missing, altered, aged out, dated ahead, or odd.

    The role named in the token picks the key that must have signed it, so a
    token signed with one person's key never names the other. A token dated
    further ahead than a slightly fast clock explains is refused too, so a
    clock set far ahead and put right cannot stretch the month.
    """
    # A cookie can carry any characters; the hash and the comparison take
    # ASCII only, so anything else is refused here rather than raised there.
    if not token or not token.isascii():
        return None
    parts = token.split(":")
    if len(parts) != 3:
        return None
    role, issued, signature = parts
    if role not in (Principal.STUDENT.value, Principal.PARENT.value):
        return None
    key = keys.get(Principal(role))
    if key is None:
        return None
    payload = f"{role}:{issued}"
    expected = hmac.new(key, payload.encode("ascii"), "sha256").hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    if not issued.isdigit():
        return None
    age = int(now.timestamp()) - int(issued)
    if age > SESSION_SECONDS or age < -SKEW_SECONDS:
        return None
    return Principal(role)


class SignInAttempts:
    """Wrong passphrases counted per device, so guessing is slowed without locking the house.

    A device is its network address. After ``limit`` wrong passphrases inside
    one ``cooldown`` it is asked to wait that long, then its count starts
    over; a right passphrase clears it. The count is per device, so one
    device cannot lock the others out, and it is bounded: past ``capacity``
    devices the oldest count is forgotten. Nothing typed is kept, only
    counts and times. It lives in this process and is empty at every start.
    """

    def __init__(
        self,
        limit: int = ATTEMPT_LIMIT,
        cooldown: int = COOLDOWN_SECONDS,
        capacity: int = ATTEMPT_ADDRESSES,
    ) -> None:
        self.limit = limit
        self.cooldown = timedelta(seconds=cooldown)
        self.capacity = capacity
        self._counts: dict[str, tuple[int, datetime]] = {}
        """Per device: wrong tries so far, and when the count began or the wait began."""
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._counts)

    def wait_for(self, device: str, now: datetime) -> int:
        """Seconds this device must still wait, or zero when it may try."""
        with self._lock:
            entry = self._counts.get(device)
            if entry is None:
                return 0
            count, since = entry
            left = since + self.cooldown - now
            if left <= timedelta(0):
                del self._counts[device]
                return 0
            if count < self.limit:
                return 0
            return max(1, left.days * 86400 + left.seconds + (1 if left.microseconds else 0))

    def failed(self, device: str, now: datetime) -> None:
        """One more wrong passphrase from this device."""
        with self._lock:
            entry = self._counts.pop(device, None)
            if entry is not None and entry[0] < self.limit and now - entry[1] < self.cooldown:
                count = entry[0] + 1
                # Reaching the limit starts the wait from now.
                since = now if count >= self.limit else entry[1]
            else:
                count, since = 1, now
                if len(self._counts) >= self.capacity:
                    del self._counts[next(iter(self._counts))]
            self._counts[device] = (count, since)

    def cleared(self, device: str) -> None:
        """A right passphrase from this device; its count is forgotten."""
        with self._lock:
            self._counts.pop(device, None)


def device_of(request: Request) -> str:
    """The device asking, by its network address, the one thing that tells devices apart."""
    return request.client.host if request.client else "unknown"


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
    What a signed-in person is shown is marked not to be stored, so a shared
    browser or anything on the way keeps no copy to show after a sign-out.
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
        keys: Mapping[Principal, bytes] = request.app.state.household_keys
        role = read_token(request.cookies.get(COOKIE), keys, datetime.now(UTC))
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
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
