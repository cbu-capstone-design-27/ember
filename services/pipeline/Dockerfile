# Build from the repo root: docker build -f services/pipeline/Dockerfile .
FROM python:3.12-slim

WORKDIR /opt/ember
COPY services/pipeline/requirements.txt services/pipeline/requirements.txt
RUN pip install --no-cache-dir -r services/pipeline/requirements.txt
COPY packages/ingestion-envelope packages/ingestion-envelope
COPY services/pipeline services/pipeline

WORKDIR /opt/ember/services/pipeline
USER nobody
ENTRYPOINT ["python", "-m", "loader"]
