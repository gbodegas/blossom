"""Runtime configuration, resolved once from the environment.

Reads the ``BLOSSOM_*`` variables documented in ``.env.example``. Relative
values resolve against the repository root, not the current working directory,
so ``.local/blossom.sqlite3`` names the same file wherever the process is
launched; absolute values are used as given.

The template and static directories come from ``__file__`` and are not
configurable: they ship with the package, and a deployment that needs to
relocate them has a packaging problem, not a configuration problem.

There is no settings library. Four paths do not justify a dependency, and
``.env`` files can be loaded with ``uv run --env-file .env``.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
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
CHECKPOINT_PATH_VARIABLE = "BLOSSOM_CHECKPOINT_PATH"
CHROMA_PATH_VARIABLE = "BLOSSOM_CHROMA_PATH"
TODAY_VARIABLE = "BLOSSOM_TODAY"
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"

# LangChain and langsmith decide whether to ship traces to a hosted service by
# reading TRACING and TRACING_V2 under both the LANGSMITH and LANGCHAIN
# prefixes, LANGSMITH first. All four spellings are written, because leaving
# any one of them alone leaves a way to turn hosted tracing on. This system
# handles a child's school planning, so traces stay on this machine.
HOSTED_TRACING_VARIABLES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)

# A second lock. If hosted tracing were ever enabled by a route the variables
# above do not govern, langsmith honors these two and sends runs without their
# inputs and outputs, so a student's assignments would still not leave.
TRACE_PAYLOAD_VARIABLES = (
    "LANGSMITH_HIDE_INPUTS",
    "LANGSMITH_HIDE_OUTPUTS",
)


def enforce_local_only_tracing(environ: "os._Environ[str] | dict[str, str] | None" = None) -> None:
    """Force hosted tracing off in ``environ`` (the process environment by default).

    Called at application startup, in the lifespan, before any store or model
    client is built, so a tracing variable set in a shell cannot turn on remote
    tracing. Not called at import or construction time: importing
    ``blossom.app`` or calling ``create_app`` leaves the environment alone.

    Writing ``false`` rather than deleting the variables matters: the framework
    treats ``false`` as unset, and a legacy ``LANGCHAIN_TRACING=true`` left in
    place would make every model call raise. The payload variables are written
    ``true`` as a second lock behind the first.
    """
    target = os.environ if environ is None else environ
    for variable in HOSTED_TRACING_VARIABLES:
        target[variable] = "false"
    for variable in TRACE_PAYLOAD_VARIABLES:
        target[variable] = "true"
    if environ is not None:
        return
    # langsmith caches environment reads for the life of the process, so a
    # check that ran before this call would keep reporting the old value.
    # Clearing that cache is what makes the setting take effect regardless of
    # ordering. The import is local and tolerant: it is only here to reach the
    # cache, and settings must import cleanly without the model framework.
    try:
        from langsmith import utils as langsmith_utils
    except ImportError:
        return
    cache_clear = getattr(getattr(langsmith_utils, "get_env_var", None), "cache_clear", None)
    if cache_clear is not None:
        cache_clear()


def resolve_configured_path(value: str) -> Path:
    """Resolve a configured path, treating relative values as repository-relative."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (REPOSITORY_ROOT / candidate).resolve()


@dataclass(frozen=True)
class Settings:
    """Runtime configuration: the filesystem locations the application needs as
    absolute paths, the optional pinned clock, and the optional model API key."""

    fixture_path: Path
    database_path: Path
    checkpoint_path: Path
    """From ``BLOSSOM_CHECKPOINT_PATH``. A graph's checkpoints live in their own
    SQLite file, apart from project state, so the two writers never contend and
    deleting a thread touches nothing else."""
    chroma_path: Path
    today: date | None = None
    """Pins the clock when set, from ``BLOSSOM_TODAY``. ``None`` means use the system clock."""
    anthropic_api_key: str | None = field(default=None, repr=False)
    """From ``ANTHROPIC_API_KEY``. Excluded from ``repr`` so it never reaches a log line.
    ``None`` is valid: nothing calls a model until a graph is built, and the app
    serves every fixture-backed page without a key."""

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

        key = source.get(ANTHROPIC_API_KEY_VARIABLE)
        anthropic_api_key = key.strip() if key is not None and key.strip() else None

        local = REPOSITORY_ROOT / ".local"
        return cls(
            fixture_path=read(FIXTURE_PATH_VARIABLE, REPOSITORY_ROOT / "data" / "synthetic"),
            database_path=read(DATABASE_PATH_VARIABLE, local / "blossom.sqlite3"),
            checkpoint_path=read(CHECKPOINT_PATH_VARIABLE, local / "checkpoints.sqlite3"),
            chroma_path=read(CHROMA_PATH_VARIABLE, local / "chroma"),
            today=today,
            anthropic_api_key=anthropic_api_key,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment on first call.

    Cached because configuration does not change while the process runs. Tests
    that need a different configuration should construct ``Settings`` directly
    rather than clearing this cache.
    """
    return Settings.from_environment()
