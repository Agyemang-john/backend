# #!/usr/bin/env bash
set -e

python manage.py collectstatic --noinput
python manage.py migrate --noinput

exec "$@"

!/usr/bin/env bash
