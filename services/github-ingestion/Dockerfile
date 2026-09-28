FROM python:3.12-slim

WORKDIR /opt/github-ingestion

COPY services/github-ingestion/envelope.py services/github-ingestion/receiver.py ./

ENV GITHUB_INGESTION_PORT=8080
EXPOSE 8080

USER nobody
CMD ["python3", "receiver.py"]
