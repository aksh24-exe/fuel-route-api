#!/bin/sh
set -eu

mkdir -p "$(dirname "$SQLITE_PATH")"

python manage.py migrate --noinput
python manage.py load_fuel_prices

exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-2}" \
    --timeout 60 \
    --access-logfile -
