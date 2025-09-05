#!/usr/bin/env bash
set -e

# Wait for Redis (and DB if using TCP) as needed
# Example for Postgres via TCP (skip if using Neon over Internet and it's reliable):
# until nc -z -w3 db 5432; do echo "Waiting for DB..."; sleep 2; done

python manage.py collectstatic --noinput
python manage.py migrate --noinput

exec "$@"
