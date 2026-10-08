# syntax=docker/dockerfile:1

# Stage 1: Build Frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --prefer-offline

COPY frontend/ ./
RUN npm run build

# Stage 2: Python Backend Runtime
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATABASE_URL=sqlite:///data/omsguru_forward_scan.db

WORKDIR /app

# curl + ca-certificates: health checks and HTTPS; postgresql-client: pg_dump / pg_restore for the backups;
# rclone: the offsite copy of the backups (Google Drive, see README "Backups & restore")
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    sqlite3 \
    postgresql-client \
    rclone \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy compiled frontend from builder
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist/

# Ensure directories for sqlite database and backups exist
RUN mkdir -p /app/data /app/data/backups

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "backend/run.py", "--host", "0.0.0.0", "--port", "8000"]
