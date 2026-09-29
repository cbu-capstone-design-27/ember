FROM python:3.12-slim

WORKDIR /opt/slack-ingestion

COPY services/slack-ingestion/envelope.py services/slack-ingestion/receiver.py ./

ENV SLACK_INGESTION_PORT=8082
EXPOSE 8082

USER nobody
CMD ["python3", "receiver.py"]
