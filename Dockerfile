# multi-stage Dockerfile for quick, minimal production image

# 1. build the React frontend
# The frontend stage runs on the build host's platform, not the target's: the
# built assets are platform-independent, and running npm ci under QEMU for
# arm64 hits npm #4828 (optional native deps missing in cross-platform builds).
FROM --platform=$BUILDPLATFORM node:22.20.0-slim AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# copy the rest of the frontend code and build
COPY frontend/ .
RUN npm run build


# 2. production Python image
FROM python:3.12.12-slim-bookworm AS backend
WORKDIR /app

# pg_dump / pg_restore for backup & restore (see app/backup_pg.py)
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# install runtime dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# copy backend source code
COPY app/ ./app/
# include smoke-test utilities so that the validation endpoint works
# (this directory is only used by the `/validate-queue` route and various
# development helpers).
COPY smoke_test/ ./smoke_test/
# maintenance scripts (e.g. encrypt_secret.py for headless credential updates)
COPY scripts/ ./scripts/
COPY README.md ./
# copy anything else the application might need (templates, etc.).
# the `static` folder is optional; we create an empty directory in the repo
# so that the COPY always succeeds even when there is nothing to add.
COPY static/ ./static/

# copy the compiled frontend assets from the builder stage
# the backend expects the build to live under frontend/dist so that
# `app.main` can mount /assets and serve index.html
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# expose the port our FastAPI server listens on
EXPOSE 8000

# production builds default to production mode; override with
# QUICKLY_MODE=development in your .env / environment if needed.
ENV QUICKLY_MODE=production
# Inbox UI hides CNAME-to-Quickly custom domains; use Beacon instead. Dev compose
# and host-Caddy stacks set QUICKLY_PREBUILT_IMAGE=0 (or QUICKLY_TRACKING_CNAME_UI=1).
ENV QUICKLY_PREBUILT_IMAGE=1

# assumes the default --port 8000 from CMD below
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# default command; environment variables (DATABASE_URL etc.) are supplied
# at runtime rather than baked into the image
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
