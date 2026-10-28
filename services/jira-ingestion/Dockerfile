FROM python:3.12-slim

WORKDIR /opt/jira-ingestion

# The enrich worker's libraries (EMBER-66). receiver.py and pull_test.py use the standard library only.
COPY services/jira-ingestion/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY services/jira-ingestion/envelope.py services/jira-ingestion/receiver.py services/jira-ingestion/pull_test.py \
     services/jira-ingestion/models.py services/jira-ingestion/enrich.py ./

ENV JIRA_INGESTION_PORT=8081
EXPOSE 8081

USER nobody
# Default: the stub receiver. The same image runs the enrich worker (`python3 enrich.py`), which is
# what the cluster deploys (EMBER-109); intake receives Jira's webhooks.
CMD ["python3", "receiver.py"]
