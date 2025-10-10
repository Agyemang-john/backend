# #!/usr/bin/env bash
# set -e

# python manage.py collectstatic --noinput
# python manage.py migrate --noinput

# exec "$@"


#!/usr/bin/env bash
set -e

# Run migrations
python manage.py migrate --noinput

# Collect static files only in production
if [ "$ENV" = "production" ]; then
    python manage.py collectstatic --noinput
fi

exec "$@"
