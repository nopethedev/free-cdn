"""Cascade application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db, jobs
from .config import settings
from .deps import (
    CSRF_EXEMPT_PREFIXES,
    SAFE_METHODS,
    LoginRequired,
    PermissionDenied,
    csrf_valid,
    get_auth,
    same_origin,
)
from .routers import admin, api, auth, public
from .routers import oauth as oauth_routes
from .web import render

logger = logging.getLogger("cascade")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure()
    db.init_db()
    jobs.start()
    logger.info("Cascade %s ready at %s", __version__, settings.data_dir)
    try:
        yield
    finally:
        await jobs.stop()


app = FastAPI(
    title="Cascade",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method not in SAFE_METHODS:
        if not same_origin(request):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(CSRF_EXEMPT_PREFIXES):
            auth_ctx = get_auth(request)
            if not csrf_valid(request, auth_ctx):
                return JSONResponse({"detail": "invalid CSRF token"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "interest-cohort=()")
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(LoginRequired)
async def handle_login_required(request: Request, exc: LoginRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    next_url = request.url.path
    if request.url.query:
        next_url += "?" + request.url.query
    return RedirectResponse(f"/login?next={next_url}", status_code=303)


@app.exception_handler(PermissionDenied)
async def handle_permission_denied(request: Request, exc: PermissionDenied):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": str(exc)}, status_code=403)
    return render(
        request,
        "error.html",
        title="Not allowed",
        message=str(exc) or "You do not have access to this page.",
        status_code=403,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    message = errors[0].get("msg", "invalid request") if errors else "invalid request"
    field = ".".join(str(part) for part in errors[0].get("loc", [])[1:]) if errors else ""
    detail = f"{field}: {message}" if field else message
    return JSONResponse({"detail": detail}, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def handle_http(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return render(
            request,
            "error.html",
            title=f"Error {exc.status_code}",
            message=str(exc.detail),
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    logger.exception("unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "internal server error"}, status_code=500)
    return render(
        request,
        "error.html",
        title="Something went wrong",
        message="The server hit an unexpected error. Check the logs for details.",
        status_code=500,
    )


app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
app.include_router(auth.router)
app.include_router(oauth_routes.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(public.router)
