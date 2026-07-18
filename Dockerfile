# Perseus Cloud API — self-contained production image.
# The released Vault binary avoids requiring a private source checkout during
# deployment and gives the API the same tested MCP server used elsewhere.
FROM python:3.12-slim

ARG PERSEUS_VAULT_VERSION=2.20.2

# Install runtime dependencies and the verified Vault release binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN curl --fail --location --retry 3 \
      "https://github.com/Perseus-Computing-LLC/perseus-vault/releases/download/v${PERSEUS_VAULT_VERSION}/perseus-vault-x86_64-unknown-linux-gnu.tar.gz" \
      -o /tmp/perseus-vault.tar.gz \
    && tar -xzf /tmp/perseus-vault.tar.gz -C /tmp \
    && install -m 0755 "$(find /tmp -type f -name perseus-vault -print -quit)" /usr/local/bin/perseus-vault \
    && rm -rf /tmp/perseus-vault /tmp/perseus-vault.tar.gz

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py .

# Create data directory
RUN mkdir -p /data

# Cloud Run sets PORT env var
ENV PORT=8080
ENV MIMIR_BINARY_PATH=/usr/local/bin/perseus-vault
ENV MIMIR_DB_PATH=/data/perseus-vault.db
ENV DATABASE_PATH=/data/perseus_cloud.db

EXPOSE 8080

# Verification links carry one-time tokens in their query strings. Disable
# Uvicorn's raw access log so those secrets never reach container log storage.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
