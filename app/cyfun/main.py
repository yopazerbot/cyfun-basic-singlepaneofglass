"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, db, scheduler
from .config import Settings, get_settings
from .framework import LEVELS, load_framework
from .security import SecurityMiddleware
from .views import render

HERE = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.evidence_dir.mkdir(parents=True, exist_ok=True)
        settings.snapshots_dir.mkdir(parents=True, exist_ok=True)
        db.init_engine(f"sqlite:///{settings.db_path.as_posix()}")
        db.create_schema()
        for level in LEVELS:
            load_framework(level)
        auth.ensure_default_admin(settings)
        if settings.scheduler_enabled:
            scheduler.start(settings)
        yield
        scheduler.shutdown()

    app = FastAPI(title=settings.app_name, version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    from .routers import actions, activity, assessment, assets, audit, connectors, dashboard, documents, evidence, journey, risk

    app.include_router(auth.router)
    for r in (dashboard, journey, risk, assessment, assets, documents, evidence, actions, connectors, audit, activity):
        app.include_router(r.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.exception_handler(auth.PasswordChangeRequired)
    async def password_change_required(request: Request, exc: auth.PasswordChangeRequired):
        if request.headers.get("hx-request"):
            return JSONResponse({"detail": "Password change required"}, status_code=403, headers={"HX-Redirect": "/auth/password"})
        nxt = request.url.path if request.method == "GET" else "/"
        return RedirectResponse(f"/auth/password?next={nxt}", status_code=303)

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            if request.headers.get("hx-request"):
                return JSONResponse({"detail": "Sign in required"}, status_code=401, headers={"HX-Redirect": "/auth/login"})
            nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/auth/login?next={nxt}", status_code=302)
        if request.headers.get("hx-request") or "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        request.state.user = getattr(request.state, "user", None)
        return render(request, "error.html", {"status": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
