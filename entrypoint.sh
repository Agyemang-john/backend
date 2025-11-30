#!/bin/sh
set -e  # exit immediately if a command fails

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn..."
exec "$@"  # run the CMD from Dockerfile
