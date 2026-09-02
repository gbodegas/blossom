"""Runtime configuration, resolved once from the environment.

Reads the ``BLOSSOM_*`` variables documented in ``.env.example``. Relative
values resolve against the repository root, not the current working directory,
so ``.local/blossom.sqlite3`` names the same file wherever the process is
launched; absolute values are used as given.

The template and static directories come from ``__file__`` and are not
configurable: they ship with the package, and a deployment that needs to
relocate them has a packaging problem, not a configuration problem.

There is no settings library. Three paths do not justify a dependency, and
``.env`` files can be loaded with ``uv run --env-file .env``.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent

# Packaged assets. Module-level so importers do not need to read the
# environment just to locate a template directory that cannot be configured.
STATIC_PATH = PACKAGE_ROOT / "static"
TEMPLATE_PATH = PACKAGE_ROOT / "templates"

FIXTURE_PATH_VARIABLE = "BLOSSOM_FIXTURE_PATH"
DATABASE_PATH_VARIABLE = "BLOSSOM_DATABASE_PATH"
CHROMA_PATH_VARIABLE = "BLOSSOM_CHROMA_PATH"
TODAY_VARIABLE = "BLOSSOM_TODAY"


def resolve_configured_path(value: str) -> Path:
    """Resolve a configured path, treating relative values as repository-relative."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (REPOSITORY_ROOT / candidate).resolve()


@dataclass(frozen=True)
class Settings:
    """Every filesystem location the application needs, as absolute paths."""

    fixture_path: Path
    database_path: Path
    chroma_path: Path
    today: date | None = None
    """Pins the clock when set, from ``BLOSSOM_TODAY``. ``None`` means use the system clock."""

    @property
    def static_path(self) -> Path:
        """Directory of packaged static assets. Not configurable; see module docstring."""
        return STATIC_PATH

    @property
    def template_path(self) -> Path:
        """Directory of packaged Jinja templates. Not configurable; see module docstring."""
        return TEMPLATE_PATH

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from ``environ``, defaulting to ``os.environ``.

        The ``environ`` argument exists so tests can supply a mapping instead of
        mutating process state.
        """
        source = os.environ if environ is None else environ

        def read(variable: str, default: Path) -> Path:
            value = source.get(variable)
            return default if value is None or not value.strip() else resolve_configured_path(value)

        pinned = source.get(TODAY_VARIABLE)
        today = None
        if pinned is not None and pinned.strip():
            try:
                today = date.fromisoformat(pinned.strip())
            except ValueError as error:
                msg = f"{TODAY_VARIABLE} must be an ISO date such as 2026-08-19, got {pinned!r}"
                raise ValueError(msg) from error

        local = REPOSITORY_ROOT / ".local"
        return cls(
            fixture_path=read(FIXTURE_PATH_VARIABLE, REPOSITORY_ROOT / "data" / "synthetic"),
            database_path=read(DATABASE_PATH_VARIABLE, local / "blossom.sqlite3"),
            chroma_path=read(CHROMA_PATH_VARIABLE, local / "chroma"),
            today=today,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment on first call.

    Cached because configuration does not change while the process runs. Tests
    that need a different configuration should construct ``Settings`` directly
    rather than clearing this cache.
    """
    return Settings.from_environment()
