"""Application assembly: three route trees over one set of stores.

The student, parent and verifier routers are mounted separately rather than
sharing a router with permission checks on individual endpoints. Separate trees
mean a handler cannot accidentally serve the wrong projection, because it has
no access to another principal's view model.

Stores are opened by the lifespan handler, not at import, so importing this
module has no side effects.
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from blossom.dependencies import create_lifespan
from blossom.routes import parent, student, verifier
from blossom.settings import Settings, enforce_local_only_tracing, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    ``settings`` lets a test point the app at a different fixture set without
    touching environment variables; it defaults to the process-wide settings.
    ``TestClient`` only runs the lifespan as a context manager, so tests use
    ``with TestClient(app) as client:``.

    Hosted tracing for the model framework is forced off here, before anything
    else is built, so the process cannot ship traces off this machine.
    """
    enforce_local_only_tracing()
    resolved = get_settings() if settings is None else settings
    app = FastAPI(title="Blossom", lifespan=create_lifespan(resolved))
    app.mount("/static", StaticFiles(directory=resolved.static_path), name="static")
    app.include_router(student.router)
    app.include_router(parent.router)
    app.include_router(verifier.router)
    return app


app = create_app()
