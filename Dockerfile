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

# HTTP: threaded WSGI workers. Each of the 2 processes serves 8 requests at
# once, so a slow photo upload to storage no longer freezes a whole worker
# (under ASGI every plain Django view in a process shares one thread).
# Workers restart after ~1000 requests to give memory back.
# Websockets run in their own small ASGI container (the `ws` service).
# --forwarded-allow-ips="*": Nginx reaches us over Docker's bridge network.
CMD ["gunicorn", "ecommerce.wsgi:application", "-k", "gthread", "--workers", "2", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "120", "--graceful-timeout", "30", "--max-requests", "1000", "--max-requests-jitter", "100", "--forwarded-allow-ips", "*"]
