"""Доступ к панели: без входа данных не видно, вход, выход, блокировка перебора."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from callscope.web import create_app

API_PREFIXES = ("/api/", "/audio/")


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _all_routes(app) -> list[tuple[str, str]]:
    """Все (метод, путь) приложения, включая подключённые роутеры (у новых FastAPI они
    лежат обёрткой с `original_router`)."""
    found: list[tuple[str, str]] = []

    def walk(routes, prefix: str) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.extend((m, prefix + route.path) for m in sorted(route.methods))
            elif isinstance(route, Mount):
                assert route.path == "/static", f"неожиданная точка монтирования {route.path}"
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes, prefix + route.include_context.prefix)
            else:
                raise AssertionError(f"неизвестный тип маршрута {type(route)}")

    walk(app.routes, "")
    return found


def test_every_route_requires_login(app, client, web_settings):
    # Есть данные — значит утечка была бы видна.
    from callscope.storage import Storage

    record, _ = Storage(web_settings.data_dir).add_upload("ivanov_2026-10-12_1530.m4a", b"x")
    routes = _all_routes(app)
    paths = {path for _, path in routes}
    for expected in ("/", "/upload", "/calls", "/managers", "/api/upload", "/api/queue",
                     "/audio/{call_id}", "/settings/checklist", "/logout"):
        assert expected in paths, f"нет маршрута {expected}"

    for method, path in routes:
        if path == "/login":
            continue
        url = _concrete(path).replace("/1", f"/{record.id}")
        response = client.request(method, url)
        is_api = url.startswith(API_PREFIXES) or url.endswith(".xlsx")
        if is_api:
            assert response.status_code == 401, (method, url, response.status_code)
        else:
            assert response.status_code == 303, (method, url, response.status_code)
            assert response.headers["location"].startswith("/login"), (method, url)
        assert "ivanov" not in response.text, (method, url)


def test_html_redirect_keeps_next(client):
    response = client.get("/upload?x=1")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fupload%3Fx%3D1"


def test_static_and_login_are_public(client):
    assert client.get("/login").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_correct_password_logs_in_with_long_httponly_cookie(client, login):
    response = login(client, next_url="/upload")
    assert response.status_code == 303
    assert response.headers["location"] == "/upload"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert f"max-age={30 * 24 * 3600}" in cookie
    assert client.get("/upload").status_code == 200


def test_next_cannot_point_outside(client, login):
    for bad in ("https://evil.example/", "//evil.example/x", "/\\evil.example"):
        response = login(client, next_url=bad)
        assert response.headers["location"] == "/", bad


def test_wrong_password_shows_message(client, login):
    response = login(client, password="wrong")
    assert response.status_code == 401
    assert "Неверный пароль" in response.text
    assert client.get("/upload").status_code == 303


def test_logout_ends_session(authed):
    response = authed.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert authed.get("/upload").status_code == 303


def test_lockout_after_five_wrong_attempts(app, client, login):
    clock = [1000.0]
    app.state.login_limiter.clock = lambda: clock[0]
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401
    fifth = login(client, password="wrong")
    assert fifth.status_code == 429
    # даже верный пароль не пускает, пока идёт блокировка, и сказано, на сколько
    blocked = login(client)
    assert blocked.status_code == 429
    assert "5 мин" in blocked.text
    assert client.get("/upload").status_code == 303

    clock[0] += 5 * 60 + 1
    assert login(client).status_code == 303


def test_success_resets_failed_attempts(app, client, login):
    for _ in range(4):
        login(client, password="wrong")
    assert login(client).status_code == 303
    client.post("/logout")
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401


def test_session_from_old_password_is_rejected(web_settings, tmp_path):
    from dataclasses import replace

    first = TestClient(create_app(web_settings), follow_redirects=False)
    first.post("/login", data={"password": web_settings.password})
    cookies = dict(first.cookies)

    changed = replace(web_settings, password="another-test-password")
    second = TestClient(create_app(changed), follow_redirects=False, cookies=cookies)
    assert second.get("/upload").status_code == 303


@pytest.mark.parametrize("path", ["/", "/managers", "/calls"])
def test_real_pages_render_in_shell(authed, path):
    response = authed.get(path)
    assert response.status_code == 200
    for item in ("Обзор", "Менеджеры", "Звонки", "Загрузка", "Настройки", "Выйти"):
        assert item in response.text


def test_no_external_urls_in_templates_and_static():
    from callscope.web.deps import STATIC_DIR, TEMPLATES_DIR

    files = [*TEMPLATES_DIR.glob("*.html"), *STATIC_DIR.glob("*")]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(https?:)?//[a-z0-9.-]+\.[a-z]{2,}", text, re.I), path.name


def test_demo_badge_only_in_demo(web_settings):
    from dataclasses import replace

    for demo, expected in ((False, False), (True, True)):
        client = TestClient(create_app(replace(web_settings, demo=demo)), follow_redirects=False)
        client.post("/login", data={"password": web_settings.password})
        assert ('class="demo-badge"' in client.get("/upload").text) is expected


def test_env_example_lists_all_names_without_values():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    lines = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    settings = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert set(settings) == {"CALLSCOPE_PASSWORD", "CALLSCOPE_SECRET_KEY", "CALLSCOPE_DATA_DIR",
                             "CALLSCOPE_HOST", "CALLSCOPE_PORT", "CALLSCOPE_TRUSTED_PROXIES",
                             "CALLSCOPE_COOKIE_SECURE"}
    assert all(value == "" for value in settings.values())
    assert ".env" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_forwarded_for_does_not_bypass_lockout(client, login):
    for _ in range(5):
        login(client, password="wrong")
    response = client.post(
        "/login",
        data={"password": "test-only-password"},
        headers={"X-Forwarded-For": "10.9.9.9", "X-Real-IP": "10.9.9.9"},
    )
    assert response.status_code == 429


def test_logout_revokes_copied_cookie(web_settings):
    first = TestClient(create_app(web_settings), follow_redirects=False)
    assert first.post("/login", data={"password": web_settings.password}).status_code == 303
    copied = dict(first.cookies)
    first.post("/logout")
    thief = TestClient(create_app(web_settings), follow_redirects=False, cookies=copied)
    assert thief.get("/upload").status_code == 303
    assert thief.get("/api/queue").status_code == 401


def test_new_login_after_logout_works(authed, login):
    authed.post("/logout")
    assert login(authed).status_code == 303
    assert authed.get("/upload").status_code == 200


@pytest.mark.parametrize("header", ["origin", "referer"])
def test_post_from_foreign_page_is_rejected(authed, header):
    value = "http://evil.example" if header == "origin" else "http://evil.example/page"
    response = authed.post("/logout", headers={header: value})
    assert response.status_code == 403
    assert authed.get("/upload").status_code == 200  # сессия цела — выхода не было


def test_post_from_panel_page_or_without_origin_passes(authed):
    assert authed.post("/period", data={"preset": "week", "next": "/"},
                       headers={"origin": "http://testserver"}).status_code == 303
    assert authed.post("/period", data={"preset": "week", "next": "/"}).status_code == 303


def test_api_post_from_foreign_origin_is_json_403(authed):
    response = authed.post("/api/calls/1/retry", headers={"origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")


def test_limiter_forgets_old_failures():
    from callscope.web.auth import LoginLimiter

    clock = [0.0]
    limiter = LoginLimiter(attempts=5, minutes=5, clock=lambda: clock[0])
    for number in range(100):
        limiter.failure(f"10.0.0.{number}")
    assert limiter.tracked() == 100
    clock[0] += 5 * 60 + 1
    limiter.failure("10.0.1.1")
    assert limiter.tracked() == 1


def test_secure_cookie_when_configured(web_settings):
    from dataclasses import replace

    client = TestClient(create_app(replace(web_settings, cookie_secure=True)),
                        base_url="https://testserver", follow_redirects=False)
    response = client.post("/login", data={"password": web_settings.password})
    assert "secure" in response.headers["set-cookie"].lower()
