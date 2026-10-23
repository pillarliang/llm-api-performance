FROM python:3.12-slim AS runtime-base
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy

FROM runtime-base AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TIKTOKEN_CACHE_DIR=/app/.token-cache
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY . ./
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=3)"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
