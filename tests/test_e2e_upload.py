"""Сквозная проверка панели на настоящем сервере: `callscope serve` в фоне, вход по паролю,
20 записей через `/api/upload`, ответы страниц во время обработки, дубликат, перезапуск
посреди очереди. Нужна модель whisper (small) в кэше Hugging Face — запускается только
с CALLSCOPE_SLOW=1:

    CALLSCOPE_SLOW=1 uv run pytest tests/test_e2e_upload.py -q -s
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("CALLSCOPE_SLOW") != "1", reason="медленный: CALLSCOPE_SLOW=1"
    ),
]

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "calls"
COPIES = 4  # 5 примеров × 4 копии = 20 разных по содержимому записей
RATE = 16000
PAGE_LIMIT_S = 1.0
PROCESS_TIMEOUT_S = 1500


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _decode_stereo(path: Path) -> bytes:
    """m4a → 16 кГц стерео s16 (PyAV, без системного ffmpeg)."""
    import av

    chunks: list[bytes] = []
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().tobytes())
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().tobytes())
    return b"".join(chunks)


def _variant(pcm: bytes, copy: int) -> bytes:
    """Другая громкость и обрезка начала — другое содержимое файла (другой sha256)."""
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2)
    trim = int(RATE * 0.05 * copy)
    gain = 1.0 - 0.08 * copy
    scaled = np.clip(samples[trim:].astype(np.float32) * gain, -32768, 32767)
    return scaled.astype(np.int16).tobytes()


def _make_recordings(folder: Path) -> list[Path]:
    folder.mkdir()
    made: list[Path] = []
    for source in sorted(EXAMPLES.glob("*.m4a")):
        pcm = _decode_stereo(source)
        manager, day, hhmm = source.stem.split("_")
        for copy in range(COPIES):
            name = f"{manager}_{day}_{int(hhmm) + copy:04d}.wav"
            target = folder / name
            with wave.open(str(target), "wb") as out:
                out.setnchannels(2)
                out.setsampwidth(2)
                out.setframerate(RATE)
                out.writeframes(_variant(pcm, copy))
            made.append(target)
    return made


class Server:
    def __init__(self, data_dir: Path, workdir: Path, password: str) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.data_dir = data_dir
        self.workdir = workdir
        self.password = password
        self.process: subprocess.Popen[bytes] | None = None
        self.log = workdir / "serve.log"

    def start(self) -> None:
        import httpx

        env = {**os.environ, "CALLSCOPE_PASSWORD": self.password, "CALLSCOPE_SECRET_KEY": ""}
        command = [
            sys.executable, "-m", "callscope", "serve",
            "--host", "127.0.0.1", "--port", str(self.port), "--data-dir", str(self.data_dir),
        ]
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                command, cwd=self.workdir, env=env, stdout=log, stderr=subprocess.STDOUT
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert self.process.poll() is None, self.log.read_text(errors="replace")
            try:
                if httpx.get(f"{self.base}/login", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.3)
        raise AssertionError("сервер не поднялся за 60 с")

    def kill(self) -> None:
        """Жёсткая остановка посреди обработки — как отключённое питание."""
        assert self.process is not None
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=10)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def client(self):
        import httpx

        client = httpx.Client(base_url=self.base, timeout=30)
        response = client.post("/login", data={"password": self.password, "next": "/"})
        assert response.status_code == 303, response.text
        return client


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    srv = Server(tmp_path / "data", tmp_path, secrets.token_urlsafe(12))
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _timed_get(client, url: str) -> float:
    started = time.monotonic()
    response = client.get(url)
    elapsed = time.monotonic() - started
    assert response.status_code == 200, (url, response.status_code)
    return elapsed


def test_twenty_uploads_processed_in_background_and_survive_restart(
    server: Server, tmp_path: Path
) -> None:
    recordings = _make_recordings(tmp_path / "recordings")
    assert len(recordings) == 20
    client = server.client()

    started = time.monotonic()
    ids: list[int] = []
    for path in recordings:
        with path.open("rb") as handle:
            response = client.post(
                "/api/upload", files={"file": (path.name, handle, "audio/wav")}
            )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["duplicate"] is False
        ids.append(body["id"])
    assert len(set(ids)) == 20

    with recordings[0].open("rb") as handle:
        duplicate = client.post(
            "/api/upload", files={"file": ("copy.wav", handle, "audio/wav")}
        )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["id"] == ids[0]

    # Пока идёт обработка — страницы отвечают быстрее секунды.
    timings: list[float] = []
    done = 0
    while done < 4:
        timings.append(_timed_get(client, "/"))
        timings.append(_timed_get(client, "/api/queue"))
        timings.append(_timed_get(client, "/upload"))
        counts = client.get("/api/queue").json()["counts"]
        assert counts["error"] == 0
        done = counts["done"]
        assert time.monotonic() - started < PROCESS_TIMEOUT_S
        time.sleep(0.5)
    client.close()
    assert done < 20, "очередь кончилась раньше перезапуска — проверка ничего не покажет"

    # Перезапуск посреди очереди: недообработанное подхватывается после старта.
    server.kill()
    restart_at = time.monotonic()
    server.start()
    client = server.client()
    while True:
        timings.append(_timed_get(client, "/"))
        started_q = time.monotonic()
        queue = client.get("/api/queue").json()
        timings.append(time.monotonic() - started_q)
        counts = queue["counts"]
        assert counts["error"] == 0, [i["error"] for i in queue["items"] if i["error"]]
        if counts["done"] == 20:
            break
        assert time.monotonic() - started < PROCESS_TIMEOUT_S, counts
        time.sleep(1)
    total = time.monotonic() - started

    statuses = {item["id"]: item["status"] for item in queue["items"]}
    assert all(statuses[i] == "done" for i in ids)
    assert all(item["score"] is not None for item in queue["items"])
    page = client.get(f"/calls/{ids[-1]}")
    assert page.status_code == 200
    client.close()

    worst = max(timings)
    after_restart = time.monotonic() - restart_at
    print(
        f"\n20 записей: {total:.0f} с всего (после перезапуска {after_restart:.0f} с); "
        f"запросов страниц {len(timings)}, худший ответ {worst * 1000:.0f} мс, "
        f"медиана {sorted(timings)[len(timings) // 2] * 1000:.0f} мс"
    )
    assert worst < PAGE_LIMIT_S
