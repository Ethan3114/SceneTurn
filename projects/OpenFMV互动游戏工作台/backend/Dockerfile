FROM node:24.22.3-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm AS dependencies
ENV TMPDIR=/opt/sceneturn/.intermediate
WORKDIR /opt/sceneturn
COPY backend/requirements.txt ./
RUN mkdir -p "$TMPDIR" \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --requirement requirements.txt \
    && /opt/venv/bin/python -m pip check

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk libatomic1 libstdc++6 \
    && apt-get clean \
    && groupadd --gid 10001 sceneturn \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /opt/sceneturn sceneturn \
    && install -d -m 0700 -o sceneturn -g sceneturn /opt/sceneturn/data /opt/sceneturn/.intermediate /opt/sceneturn/.intermediate/media

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/opt/sceneturn/.intermediate \
    OPENFMV_DATA_DIR=/opt/sceneturn/data \
    OPENFMV_TMP_DIR=/opt/sceneturn/.intermediate/media
WORKDIR /opt/sceneturn
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/alembic ./backend/alembic
COPY backend/app ./backend/app
COPY scripts/render_game.cjs ./scripts/render_game.cjs
COPY frontend/electron/exporter.js ./frontend/electron/exporter.js
COPY frontend/shared/assetPaths.js frontend/shared/graphRuntimeCore.mjs frontend/shared/runtimeCore.mjs ./frontend/shared/

WORKDIR /opt/sceneturn/backend
USER 10001:10001
RUN node --version && ffmpeg -version && ffprobe -version
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
