from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from blossom.routes import parent, student, verifier
from blossom.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    ``settings`` is a parameter so a test can point the application at a
    different fixture set without touching process environment variables. It
    defaults to the process-wide settings.
    """
    resolved = get_settings() if settings is None else settings
    app = FastAPI(title="Blossom")
    app.mount("/static", StaticFiles(directory=resolved.static_path), name="static")
    app.include_router(student.router)
    app.include_router(parent.router)
    app.include_router(verifier.router)
    return app


app = create_app()
