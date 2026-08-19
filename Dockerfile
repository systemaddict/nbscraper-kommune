FROM node:24-slim AS auth-build

WORKDIR /build/auth
COPY auth/package.json auth/package-lock.json ./
RUN npm ci
COPY auth/tsconfig.json ./
COPY auth/src ./src
RUN npm run build && npm prune --omit=dev

FROM python:3.12-slim

# lxml needs no build deps on slim for manylinux wheels, but curl is handy for
# container healthchecks and ca-certificates is required for HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The dashboard container runs a tiny Better Auth/Hono gateway in front of the
# existing FastAPI process. Both official images use Debian slim, so the Node
# runtime and the installed JS dependencies can be copied without another
# package repository in the final image.
COPY --from=auth-build /usr/local/bin/node /usr/local/bin/node
COPY --from=auth-build /build/auth/package.json ./auth/package.json
COPY --from=auth-build /build/auth/node_modules ./auth/node_modules
COPY --from=auth-build /build/auth/dist ./auth/dist
COPY auth/static ./auth/static

# Dependencies first so a code change does not invalidate the wheel cache.
COPY pyproject.toml README.md ./
COPY nbkommune ./nbkommune
RUN pip install --no-cache-dir -e ".[api]"

# Unbuffered so Magic Containers logs show worker progress live. Bunny injects
# database credentials at runtime; they never belong in an image layer.
ENV PYTHONUNBUFFERED=1

# The default command remains the private worker. A second Magic Container can
# reuse this image with `nbk serve --port 8000` for the status dashboard.
EXPOSE 8000

# The worker is the whole service: it seeds its own discovery tasks on boot and
# self-reschedules, so no external cron is needed.
CMD ["nbk", "worker"]
