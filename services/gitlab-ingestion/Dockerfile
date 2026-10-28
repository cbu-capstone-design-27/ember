FROM python:3.12-slim

WORKDIR /opt/gitlab-ingestion

# The enrich worker's libraries (EMBER-68). receiver.py and pull.py use the standard library only.
COPY services/gitlab-ingestion/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# One image, three entry points: the webhook receiver (default), pull.py for backfill and
# reconciliation, and the enrich worker (`python3 enrich.py`), which is what the cluster deploys
# (EMBER-83). Secrets come only from environment variables.
COPY services/gitlab-ingestion/envelope.py services/gitlab-ingestion/receiver.py services/gitlab-ingestion/pull.py \
     services/gitlab-ingestion/models.py services/gitlab-ingestion/enrich.py ./

ENV GITLAB_INGESTION_PORT=8083
EXPOSE 8083

USER nobody
CMD ["python3", "receiver.py"]
