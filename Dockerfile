FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Copy source code
COPY . .

# --forwarded-allow-ips="*" tells gunicorn to trust X-Forwarded-For from any IP.
# Required because Nginx contacts Django via Docker's bridge network (172.x.x.x),
# and gunicorn's default only trusts 127.0.0.1.
# CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "ecommerce.asgi:application", "--access-log", "-", "--proxy-headers"]
CMD ["gunicorn", "ecommerce.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--workers", "2", "--timeout", "300", "--forwarded-allow-ips", "*"]