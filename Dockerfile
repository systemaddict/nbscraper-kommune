FROM python:3.12-slim

# lxml needs no build deps on slim for manylinux wheels, but curl is handy for
# container healthchecks and ca-certificates is required for HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not invalidate the wheel cache.
COPY pyproject.toml README.md ./
COPY nbkommune ./nbkommune
RUN pip install --no-cache-dir -e .

# Unbuffered so Magic Containers logs show worker progress live. Bunny injects
# database credentials at runtime; they never belong in an image layer.
ENV PYTHONUNBUFFERED=1

# The worker is the whole service: it seeds its own discovery tasks on boot and
# self-reschedules, so no external cron is needed.
CMD ["nbk", "worker"]
