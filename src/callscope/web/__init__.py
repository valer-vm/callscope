"""Веб-панель руководителя: FastAPI, шаблоны Jinja2, без сборки фронтенда."""

from __future__ import annotations

from callscope.web.app import create_app
from callscope.web.settings import WebSettings, load_web_settings

__all__ = ["WebSettings", "create_app", "load_web_settings"]
