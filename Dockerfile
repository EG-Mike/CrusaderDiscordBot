# Crusader bot + web dashboard in one image.
#
#   docker build -t crusader-bot .
#
# Data (applications, polls, caches, crusader.db, audit.log) is written to
# the working directory /data - mount a volume there so it survives
# updates. Settings come from environment variables (an env_file) and an
# optional /app/config/deployment_local.py mounted read-only.

# --- stage 1: build the dashboard frontend -------------------------------------
# Runs on the build machine's own platform even for an arm64 image: the
# output is plain HTML/JS/CSS, identical for every platform, and emulating
# Node for it would only make multi-platform builds much slower.
FROM --platform=$BUILDPLATFORM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- stage 2: the bot ------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
COPY --from=web /web/dist /app/web/dist

# Which build this is (set by the GitHub Actions build): shown in the
# dashboard and recorded in the System log when the bot is updated.
ARG APP_VERSION=dev
ARG APP_COMMIT_TITLE=
ARG APP_BUILT_AT=
ARG APP_REPOSITORY=
ENV APP_VERSION=$APP_VERSION \
    APP_COMMIT_TITLE=$APP_COMMIT_TITLE \
    APP_BUILT_AT=$APP_BUILT_AT \
    APP_REPOSITORY=$APP_REPOSITORY \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8081 \
    BOT_DB_PATH=/data/crusader.db

# Run as an unprivileged user; /data is where everything is written.
RUN useradd --uid 1000 --create-home bot && mkdir /data && chown bot:bot /data
USER bot
WORKDIR /data
VOLUME /data
EXPOSE 8081

CMD ["python", "/app/bot.py"]
