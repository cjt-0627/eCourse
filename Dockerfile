FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

RUN pip install --no-cache-dir uv==0.9.5

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
RUN uv sync --frozen --no-dev \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

EXPOSE 8765

CMD ["ecourse", "serve", "--host", "0.0.0.0", "--port", "8765"]
