FROM python:3.12-slim

WORKDIR /opt/intake

COPY services/intake/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Webhook secrets and DATABASE_URL come only from environment variables
COPY services/intake/intake.py services/intake/models.py ./
COPY services/intake/adapters ./adapters

ENV LISTEN_PORT=8080 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080

USER nobody
CMD ["python3", "intake.py"]
