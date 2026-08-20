"""Runtime configuration, resolved once from the environment.

Two problems motivated this module.

The scaffold hardcoded three relative paths -- ``blossom/static``,
``blossom/templates`` and ``data/synthetic`` -- so the application only started
when the current working directory happened to be the repository root. Starting
it from anywhere else failed at import time, when ``StaticFiles`` checked for a
directory that was not there.

Separately, ``.env.example`` documented three ``BLOSSOM_*`` variables that no
code read. They were decorative. This module makes them real.

Two deliberate choices are worth stating.

Package assets are not configurable. The template and static directories are
resolved from ``__file__`` and cannot be overridden, because they ship with the
package and a deployment that needs to relocate them has a packaging problem
rather than a configuration problem.

Relative values are resolved against the repository root rather than the
current working directory. ``.env.example`` says ``.local/blossom.sqlite3``,
and that should mean the same location no matter which directory the process
was launched from. Absolute values are used as given.

There is no settings library here on purpose. Three paths do not justify a
dependency, and ``.env`` files can be loaded at the launcher with
``uv run --env-file .env`` without one.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent

FIXTURE_PATH_VARIABLE = "BLOSSOM_FIXTURE_PATH"
DATABASE_PATH_VARIABLE = "BLOSSOM_DATABASE_PATH"
CHROMA_PATH_VARIABLE = "BLOSSOM_CHROMA_PATH"


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

    @property
    def static_path(self) -> Path:
        """Directory of packaged static assets. Not configurable; see module docstring."""
        return PACKAGE_ROOT / "static"

    @property
    def template_path(self) -> Path:
        """Directory of packaged Jinja templates. Not configurable; see module docstring."""
        return PACKAGE_ROOT / "templates"

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

        local = REPOSITORY_ROOT / ".local"
        return cls(
            fixture_path=read(FIXTURE_PATH_VARIABLE, REPOSITORY_ROOT / "data" / "synthetic"),
            database_path=read(DATABASE_PATH_VARIABLE, local / "blossom.sqlite3"),
            chroma_path=read(CHROMA_PATH_VARIABLE, local / "chroma"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment on first call.

    Cached because configuration does not change while the process runs. Tests
    that need a different configuration should construct ``Settings`` directly
    rather than clearing this cache.
    """
    return Settings.from_environment()
