# Образ веб-панели CallScope: python:3.12-slim + uv, без ffmpeg (PyAV ставится колесом).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/hf-cache \
    CALLSCOPE_DATA_DIR=/data \
    CALLSCOPE_HOST=0.0.0.0 \
    CALLSCOPE_PORT=8000

WORKDIR /app

# Сначала только зависимости — слой кэшируется, пока не меняется uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Непривилегированный пользователь; именованные тома наследуют владельца этих папок.
RUN useradd --create-home --uid 1000 callscope \
    && mkdir -p /data /hf-cache \
    && chown callscope:callscope /data /hf-cache
USER callscope

VOLUME ["/data", "/hf-cache"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4)"]

CMD ["callscope", "serve", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data"]
