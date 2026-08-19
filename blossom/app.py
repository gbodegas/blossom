from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from blossom.routes import parent, student, verifier


def create_app() -> FastAPI:
    app = FastAPI(title="Blossom")
    app.mount("/static", StaticFiles(directory="blossom/static"), name="static")
    app.include_router(student.router)
    app.include_router(parent.router)
    app.include_router(verifier.router)
    return app


app = create_app()
