FROM python:3.12-slim

# Bytecode + stdout flushing for cleaner container behavior.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so code-only rebuilds hit the layer cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Source. .dockerignore keeps secrets, state, and macOS-only bits out.
COPY . .

# Orchestrator listens on 0.0.0.0:8787 by default (see orchestrator bottom).
EXPOSE 8787

# .env is expected to be mounted or provided via --env-file at runtime —
# never baked into the image. The `schedule_config.py` the container runs
# with should point at portable providers (NtfyNotifier, no TodoSource,
# or a web-API TodoSource) — the macOS-only providers will fail to import
# in a Linux container.
CMD ["python", "orchestrator.py"]
