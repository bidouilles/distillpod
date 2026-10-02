# Stage 1: Build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Runtime
FROM python:3.12-slim
WORKDIR /app
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno

# System deps: ffmpeg for ad-free audio cutting, Node.js + the agent CLI.
# Build with --build-arg LLM_BACKEND=claude to bake in the Claude CLI instead.
ARG LLM_BACKEND=codex
RUN apt-get update && \
    apt-get install -y curl ffmpeg git && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    if [ "$LLM_BACKEND" = "claude" ]; then \
        npm install -g @anthropic-ai/claude-code; \
    else \
        npm install -g @openai/codex; \
    fi && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt /app/backend/requirements.txt
ARG INSTALL_LOCAL_STT=true
RUN if [ "$INSTALL_LOCAL_STT" = "true" ]; then \
        cp /app/backend/requirements.txt /tmp/runtime-requirements.txt; \
    else \
        sed '/^faster-whisper/d' /app/backend/requirements.txt > /tmp/runtime-requirements.txt; \
    fi && \
    pip install --no-cache-dir -r /tmp/runtime-requirements.txt 'yt-dlp[default]'

# Copy backend source
COPY backend/ /app/backend/
COPY scripts/ /app/scripts/

# Copy built frontend from stage 1
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Create data directories
# Build contexts may come from a private (umask 077) checkout. Keep code owned
# by root but readable by the non-root runtime user.
RUN chmod -R a+rX /app/backend /app/scripts /app/frontend && \
    mkdir -p /app/media /app/reports /app/data
RUN useradd --uid 1000 --create-home app && \
    mkdir -p /home/app/.codex && chown -R app:app /app/data /app/media /app/reports /home/app

WORKDIR /app/backend

ENV LLM_BACKEND=${LLM_BACKEND}
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

USER app

EXPOSE 8124

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8124"]
