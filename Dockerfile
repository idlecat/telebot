FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to fully support YouTube's signature decryption.
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock .
RUN uv sync --frozen --no-dev --no-install-project

COPY bot.py .

ENV PATH="/app/.venv/bin:$PATH"

# Command is expected to be reachable inside the container, e.g. a script
# under /scripts mounted via docker-compose.
CMD ["python", "-u", "bot.py"]
