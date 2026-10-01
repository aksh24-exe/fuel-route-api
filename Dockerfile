FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=0 \
    SQLITE_PATH=/data/db.sqlite3 \
    DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker/entrypoint.sh

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["docker/entrypoint.sh"]
