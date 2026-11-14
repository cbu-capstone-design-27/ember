# ember-backend (EMBER-160, ADR 0012): intake and the workers, one image. Build from the repo root:
#   docker build -f services/backend/Dockerfile -t ember-backend .
FROM python:3.12-slim

WORKDIR /opt/ember

COPY services/backend/pyproject.toml ./
COPY services/backend/src ./src
RUN pip install --no-cache-dir . && rm -rf src pyproject.toml build
# Opens source_credentials rows (packages/source-credentials/README.md: each consumer copies it in)
COPY packages/source-credentials/source_credentials.py ./

# Settings and secrets come only from environment variables (README.md, Configuration)
ENV PYTHONPATH=/opt/ember LISTEN_PORT=8080 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080

USER nobody
# exec form, so the process is PID 1 and gets SIGTERM itself
ENTRYPOINT ["python3", "-m", "ember"]
CMD ["run"]
