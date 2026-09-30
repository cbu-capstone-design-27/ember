FROM python:3.12-slim

WORKDIR /opt/jira-ingestion

COPY services/jira-ingestion/envelope.py services/jira-ingestion/receiver.py ./

ENV JIRA_INGESTION_PORT=8081
EXPOSE 8081

USER nobody
CMD ["python3", "receiver.py"]
