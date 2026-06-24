FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    KIS_TOKEN_CACHE_PATH=/kis-cache/kis_token_cache.json

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /kis-cache \
    && chown -R app:app /kis-cache

USER app

ENTRYPOINT ["kis-overseas"]
CMD ["--help"]
