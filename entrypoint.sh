#!/usr/bin/env bash
set -e

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Applying migrations..."
python manage.py migrate --noinput

echo "Starting server..."
exec "$@"