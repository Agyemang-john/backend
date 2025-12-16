#!/bin/sh
set -e  # exit immediately if a command fails

echo "Making migrations..."
python manage.py makemigrations

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Uvicorn (dev server with hot reload)..."
exec uvicorn ecommerce.asgi:application --host 0.0.0.0 --port 8000 --reload
