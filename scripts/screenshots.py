"""Скриншоты панели для README из демо-режима (headless Chrome, вход по паролю через форму).

Сценарий сам создаёт временную демо-базу (`callscope demo`), поднимает `callscope serve --demo`
со случайным паролем, открывает Chrome без окна с `--remote-debugging-port`, входит через
страницу /login и снимает страницы в docs/screenshots/. Сеть наружу не нужна.

    uv run python scripts/screenshots.py [--out docs/screenshots] [--chrome <путь>]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIDTH, HEIGHT = 1280, 860


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"не отвечает: {url}")


class DevTools:
    """Минимальный клиент WebSocket (RFC 6455) для протокола Chrome DevTools — без зависимостей."""

    def __init__(self, ws_url: str) -> None:
        rest = ws_url.removeprefix("ws://")
        address, _, path = rest.partition("/")
        host, _, port = address.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /{path} HTTP/1.1\r\nHost: {address}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError(head.decode(errors="replace"))
        self.next_id = 0

    def _read(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("DevTools закрыл соединение")
            data += chunk
        return data

    def _send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _receive(self) -> dict:
        message = b""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if first & 0x0F in (0x1, 0x0):
                message += payload
                if first & 0x80:
                    return json.loads(message)

    def call(self, method: str, **params: object) -> dict:
        self.next_id += 1
        self._send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            reply = self._receive()
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def evaluate(self, expression: str) -> object:
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    def open(self, url: str) -> None:
        self.call("Page.navigate", url=url)
        time.sleep(0.3)
        deadline = time.monotonic() + 20
        while self.evaluate("document.readyState") != "complete":
            if time.monotonic() > deadline:
                raise RuntimeError(f"страница не загрузилась: {url}")
            time.sleep(0.1)
        time.sleep(0.4)

    def shot(self, path: Path, full_height: int | None = None) -> None:
        height = full_height or HEIGHT
        self.call(
            "Emulation.setDeviceMetricsOverride",
            width=WIDTH, height=height, deviceScaleFactor=1, mobile=False,
        )
        time.sleep(0.3)
        data = self.call("Page.captureScreenshot", format="png")["data"]
        path.write_bytes(base64.b64decode(data))
        self.call(
            "Emulation.setDeviceMetricsOverride",
            width=WIDTH, height=HEIGHT, deviceScaleFactor=1, mobile=False,
        )


def _audio_call_id(data_dir: Path) -> int:
    """Звонок с настоящим аудио из examples/calls (в демо они .m4a), лучше — с правкой."""
    from callscope.storage import Storage

    calls = [c for c in Storage(data_dir).list_calls() if c.file_name.endswith(".m4a")]
    if not calls:
        raise RuntimeError("в демо-базе нет звонков с настоящим аудио")
    calls.sort(key=lambda c: (-len(c.corrections), c.id))
    return calls[0].id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE.parent / "docs" / "screenshots")
    parser.add_argument("--chrome", default=DEFAULT_CHROME)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="callscope-shots-"))
    data_dir = workdir / "demo"
    password = secrets.token_urlsafe(12)
    env = {**os.environ, "CALLSCOPE_PASSWORD": password, "CALLSCOPE_SECRET_KEY": ""}
    callscope = [sys.executable, "-m", "callscope"]
    server = chrome = None
    try:
        subprocess.run(
            [*callscope, "demo", "--data-dir", str(data_dir)], cwd=workdir, env=env, check=True
        )
        call_id = _audio_call_id(data_dir)
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [*callscope, "serve", "--demo", "--data-dir", str(data_dir), "--port", str(port)],
            cwd=workdir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_http(f"{base}/login")

        debug_port = _free_port()
        chrome = subprocess.Popen(
            [
                args.chrome, "--headless=new", f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={workdir / 'chrome'}", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
                f"--window-size={WIDTH},{HEIGHT}", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_http(f"http://127.0.0.1:{debug_port}/json/version")
        with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list") as response:
            page = next(t for t in json.load(response) if t["type"] == "page")
        tools = DevTools(page["webSocketDebuggerUrl"])
        tools.call("Page.enable")
        tools.call(
            "Emulation.setDeviceMetricsOverride",
            width=WIDTH, height=HEIGHT, deviceScaleFactor=1, mobile=False,
        )

        # Вход — как у человека: пароль в форму и «Войти».
        tools.open(f"{base}/login")
        tools.evaluate(
            "document.querySelector('input[name=password]').value = "
            f"{json.dumps(password)}; document.querySelector('form').submit();"
        )
        time.sleep(1.0)
        tools.open(f"{base}/")
        if "/login" in str(tools.evaluate("location.pathname")):
            raise RuntimeError("вход не удался")

        pages = [
            ("overview.png", "/", 1500),
            ("managers.png", "/managers", 540),
            ("manager.png", "/managers/ivanov", 1100),
            ("call.png", f"/calls/{call_id}", 1200),
            ("upload.png", "/upload", None),
            ("checklist.png", "/settings/checklist", None),
        ]
        for name, path, height in pages:
            tools.open(f"{base}{path}")
            tools.shot(args.out / name, height)
            size = (args.out / name).stat().st_size // 1024
            print(f"{name}: {path} ({size} КБ)")
        return 0
    finally:
        for process in (chrome, server):
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
