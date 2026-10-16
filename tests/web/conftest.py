"""Общее для тестов панели: настройки на tmp_path, приложение, вход."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from callscope.web import create_app
from callscope.web.settings import WebSettings

PASSWORD = "test-only-password"


@pytest.fixture
def web_settings(tmp_path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path / "data", password=PASSWORD, secret_key="test-secret-key"
    )


@pytest.fixture
def app(web_settings):
    return create_app(web_settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient, password: str = PASSWORD, next_url: str | None = None):
    data = {"password": password}
    if next_url is not None:
        data["next"] = next_url
    return client.post("/login", data=data)


@pytest.fixture
def login():
    """login(client, password=PASSWORD, next_url=None) → ответ POST /login."""
    return _login


@pytest.fixture
def authed(client) -> TestClient:
    response = _login(client)
    assert response.status_code == 303
    return client
