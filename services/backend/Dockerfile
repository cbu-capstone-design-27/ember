FROM python:3.12-slim
# dbmate, for the migrate Job: the same image and tag as the backend, so the schema and the code that
# reads it ship together. Pinned and checksummed per architecture (the image is built for both)
ARG TARGETARCH
ARG DBMATE_VERSION=v2.36.0
ARG DBMATE_SHA256_amd64=47e284b3d8cbad1ba5f090495aa05afd1bbd5f35e2ed5577aad06da74ce780ce
ARG DBMATE_SHA256_arm64=c7d49db4b54e487eddf8ce7d6e88f4035e1eb04c7edb16008f668cdfad2f29e8
ADD --chmod=755 https://github.com/amacneil/dbmate/releases/download/${DBMATE_VERSION}/dbmate-linux-${TARGETARCH} /usr/local/bin/dbmate
RUN sum="DBMATE_SHA256_${TARGETARCH}"; eval "sum=\$$sum"; echo "$sum  /usr/local/bin/dbmate" | sha256sum -c -
WORKDIR /opt/ember
COPY services/backend/pyproject.toml ./
COPY services/backend/src ./src
RUN pip install --no-cache-dir . && rm -rf src pyproject.toml build
COPY packages/source-credentials/source_credentials.py ./
COPY services/backend/migrations ./migrations
ENV PYTHONPATH=/opt/ember LISTEN_PORT=8080 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DBMATE_MIGRATIONS_DIR=/opt/ember/migrations DBMATE_NO_DUMP_SCHEMA=true
EXPOSE 8080
USER nobody
ENTRYPOINT ["python3", "-m", "ember"]
CMD ["run"]
