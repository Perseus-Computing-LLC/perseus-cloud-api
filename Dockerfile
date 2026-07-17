# Perseus Cloud API — Cloud Run Dockerfile
# Multi-stage build: Python API + Mimir binary

FROM rust:1.80-slim AS mimir-builder
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY mimir-repo/ .
RUN cargo build --release && cp target/release/mimir /mimir

FROM python:3.12-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Mimir binary
COPY --from=mimir-builder /mimir /usr/local/bin/mimir

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py .

# Create data directory
RUN mkdir -p /data

# Cloud Run sets PORT env var
ENV PORT=8080
ENV MIMIR_BINARY_PATH=/usr/local/bin/mimir
ENV MIMIR_DB_PATH=/data/mimir.db
ENV DATABASE_PATH=/data/perseus_cloud.db

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
