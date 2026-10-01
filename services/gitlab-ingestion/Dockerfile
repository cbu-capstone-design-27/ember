FROM python:3.12-slim

WORKDIR /opt/gitlab-ingestion

# One image, two entry points: the webhook receiver (default) and pull.py for
# backfill and reconciliation. Secrets come only from environment variables.
COPY services/gitlab-ingestion/envelope.py services/gitlab-ingestion/receiver.py services/gitlab-ingestion/pull.py ./

ENV GITLAB_INGESTION_PORT=8083
EXPOSE 8083

USER nobody
CMD ["python3", "receiver.py"]
