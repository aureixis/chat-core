FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 lamya \
    && useradd --system --uid 10001 --gid lamya --no-create-home --shell /usr/sbin/nologin lamya
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 alembic.ini ./
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 app ./app

USER lamya
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=2)" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
