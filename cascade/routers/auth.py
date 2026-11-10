"""Authentication pages."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..config import settings
from ..deps import client_ip, get_auth
from ..security import (
    DUMMY_HASH,
    SESSION_COOKIE,
    create_session,
    delete_session,
    hash_password,
    login_limiter,
    verify_password,
)
from ..utils import now
from ..web import render

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def safe_next(value: str | None, fallback: str = "/admin") -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    if value.startswith(("/login", "/setup", "/signup")):
        return fallback
    return value


def _user_count() -> int:
    row = db.fetchone("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


def _session_response(request: Request, user, days: int, redirect_to: str) -> RedirectResponse:
    with db.db() as conn:
        token, _csrf = create_session(
            conn,
            int(user["id"]),
            client_ip(request),
            request.headers.get("user-agent"),
            days,
        )
    response = RedirectResponse(redirect_to, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(1, days) * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.get("/login")
async def login_page(request: Request, next: str = "/admin", setup: str = ""):
    if get_auth(request):
        return RedirectResponse("/admin", status_code=303)
    if _user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    notice = None
    if setup == "done":
        notice = "Setup is already complete. Sign in with an existing account."
    return render(
        request,
        "login.html",
        next_url=safe_next(next),
        error=None,
        notice=notice,
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/admin"),
):
    ip = client_ip(request)
    if not login_limiter.allow(ip):
        return render(
            request,
            "login.html",
            next_url=safe_next(next),
            error="Too many attempts. Wait a few minutes and try again.",
            status_code=429,
        )
    email_value = email.strip().lower()
    user = db.fetchone("SELECT * FROM users WHERE email = ?", (email_value,))
    stored = user["password_hash"] if user else DUMMY_HASH
    valid = verify_password(password, stored)
    if not user or not valid or user["disabled"]:
        db.audit(user["id"] if user else None, "login.failed", email_value, ip)
        return render(
            request,
            "login.html",
            next_url=safe_next(next),
            error="Invalid email or password.",
            status_code=400,
        )
    days = 7
    try:
        days = int(db.get_setting("session_days", "7") or 7)
    except ValueError:
        days = 7
    db.audit(int(user["id"]), "login", user["email"], ip)
    return _session_response(request, user, days, safe_next(next))


@router.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/setup")
async def setup_page(request: Request):
    if _user_count() > 0:
        return RedirectResponse("/login?setup=done", status_code=303)
    return render(request, "setup.html", error=None)


@router.post("/setup")
async def setup_submit(
    request: Request,
    name: str = Form("Administrator"),
    email: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
):
    if _user_count() > 0:
        return RedirectResponse("/login?setup=done", status_code=303)
    email_value = email.strip().lower()
    error = None
    if not EMAIL_RE.match(email_value):
        error = "Enter a valid email address."
    elif len(password) < 8:
        error = "Use at least 8 characters for the password."
    elif password != confirm:
        error = "The passwords do not match."
    if error:
        return render(request, "setup.html", error=error, status_code=400, form={"name": name, "email": email})
    with db.db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (email, name, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
            (email_value, name.strip()[:80] or "Administrator", hash_password(password), now()),
        )
        user_id = cursor.lastrowid
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    db.audit(user_id, "setup.admin_created", email_value, client_ip(request))
    return _session_response(request, user, 7, "/admin")


@router.get("/signup")
async def signup_page(request: Request):
    if get_auth(request):
        return RedirectResponse("/admin", status_code=303)
    if _user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if db.get_setting("public_signup", "0") != "1":
        return render(
            request,
            "login.html",
            next_url="/admin",
            error="Sign-up is disabled. Ask an administrator for an account.",
            status_code=403,
        )
    return render(request, "signup.html", error=None, form={})


@router.post("/signup")
async def signup_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
):
    if _user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if db.get_setting("public_signup", "0") != "1":
        return render(request, "signup.html", error="Sign-up is disabled.", status_code=403, form={})
    email_value = email.strip().lower()
    form = {"name": name, "email": email}
    if not EMAIL_RE.match(email_value):
        return render(request, "signup.html", error="Enter a valid email address.", status_code=400, form=form)
    if len(password) < 8:
        return render(request, "signup.html", error="Use at least 8 characters.", status_code=400, form=form)
    if db.fetchone("SELECT id FROM users WHERE email = ?", (email_value,)):
        return render(request, "signup.html", error="That email already has an account.", status_code=409, form=form)
    with db.db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (email, name, password_hash, role, created_at) VALUES (?, ?, ?, 'user', ?)",
            (email_value, (name.strip() or email_value.split("@")[0])[:80], hash_password(password), now()),
        )
        user_id = cursor.lastrowid
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    db.audit(user_id, "signup", email_value, client_ip(request))
    return _session_response(request, user, 7, "/admin")
