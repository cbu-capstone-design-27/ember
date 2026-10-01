FROM python:3.12-slim

WORKDIR /opt/slack-ingestion

COPY services/slack-ingestion/envelope.py services/slack-ingestion/receiver.py services/slack-ingestion/pull_test.py ./

# Backfill output lands here. Owned by nobody so a named volume mounted on it is writable.
RUN mkdir /data && chown nobody:nogroup /data

ENV SLACK_INGESTION_PORT=8082
EXPOSE 8082

USER nobody
# Default: the always-on receiver. The backfill uses the same image with
# `python3 pull_test.py ...` (compose service slack-backfill).
CMD ["python3", "receiver.py"]
