FROM python:3.12-slim

WORKDIR /opt/github-ingestion

# The App JWT is signed with the openssl binary (pull_test.openssl_sign); the stdlib has no RSA signer
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

COPY services/github-ingestion/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# One image, two entry points: enrich:github (default) and pull_test.py for a local backfill.
# DATABASE_URL, GITHUB_APP_ID and the App key come only from the environment or a mounted file.
COPY services/github-ingestion/envelope.py services/github-ingestion/models.py \
     services/github-ingestion/pull_test.py services/github-ingestion/enrich.py ./

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

USER nobody
CMD ["python3", "enrich.py"]
