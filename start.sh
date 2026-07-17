#!/bin/bash
# Perseus Cloud API — startup script
# Start with: bash /opt/data/webui/perseus-cloud-api/start.sh
set -e

cd /opt/data/webui/perseus-cloud-api

# Load env vars from .env file (from filesystem, avoids credential redaction in shell)
set -a
source .env
set +a

echo "Starting Perseus Cloud API on port 8080..."
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8080
