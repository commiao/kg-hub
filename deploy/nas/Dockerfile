# kg_hub_server image — runs the Starlette/uvicorn HTTP API (/api/ingest, /api/search).
# Build context MUST be the repo root (kg-hub/). See docker-compose.yml (context: ../..).
FROM python:3.13-slim

WORKDIR /app

# libgomp1: required by onnxruntime/fastembed (OpenMP runtime) for local embeddings.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install deps first for layer caching.
COPY deploy/nas/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# App code. .dockerignore keeps out .venv / data / .git / logs / spike-graphiti.
COPY . /app

# EMBEDDING_DIM must be set before graphiti_core import (frozen pydantic field).
ENV EMBEDDING_DIM=384 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# kg_hub_server.py runs uvicorn itself and blocks on FalkorDB readiness (up to 90s).
CMD ["python", "kg_hub_server.py"]
