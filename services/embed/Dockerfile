FROM python:3.12-slim

WORKDIR /opt/embed

COPY services/embed/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# DATABASE_URL, EMBEDDING_URL and EMBEDDING_API_KEY come only from the environment
COPY services/embed/embed.py services/embed/models.py ./

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

USER nobody
CMD ["python3", "embed.py"]
