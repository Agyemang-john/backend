#!/usr/bin/env bash
set -e

echo "Running Django setup..."
python manage.py collectstatic --noinput
python manage.py migrate --noinput

echo "Starting server..."
exec "$@"
