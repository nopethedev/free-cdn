"""Template rendering shared by the page routers."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import settings
from .db import get_settings_map
from .utils import fmt_time, human_size, iso_time, rel_time

templates = Jinja2Templates(directory=str(settings.template_dir))
templates.env.filters["human_size"] = human_size
templates.env.filters["fmt_time"] = fmt_time
templates.env.filters["iso_time"] = iso_time
templates.env.filters["rel_time"] = rel_time
templates.env.globals["app_version"] = __version__


def render(
    request: Request,
    name: str,
    *,
    auth=None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **context: Any,
):
    values = get_settings_map()
    context.setdefault("site_name", values.get("site_name") or "Cascade")
    context.setdefault("tagline", values.get("tagline") or "")
    context.setdefault("accent", values.get("accent") or "#7c5cff")
    context.setdefault("config_values", values)
    context["user"] = auth.user if auth else None
    context["csrf"] = auth.csrf if auth else ""
    context.setdefault("active", "")
    context["request"] = request
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
    if headers:
        response.headers.update(headers)
    return response
