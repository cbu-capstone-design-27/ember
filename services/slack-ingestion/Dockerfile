FROM python:3.12-slim

WORKDIR /opt/slack-ingestion

# The enrich worker's libraries (EMBER-67). receiver.py and pull_test.py use the standard library only.
COPY services/slack-ingestion/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY services/slack-ingestion/envelope.py services/slack-ingestion/receiver.py services/slack-ingestion/pull_test.py \
     services/slack-ingestion/models.py services/slack-ingestion/pg_gates.py services/slack-ingestion/enrich.py ./

# Backfill output lands here. Owned by nobody so a named volume mounted on it is writable.
RUN mkdir /data && chown nobody:nogroup /data

ENV SLACK_INGESTION_PORT=8082
EXPOSE 8082

USER nobody
# Default: the always-on receiver. The same image runs the backfill (`python3 pull_test.py ...`,
# compose service slack-backfill) and the enrich worker (`python3 enrich.py`, compose service slack-enrich).
CMD ["python3", "receiver.py"]
