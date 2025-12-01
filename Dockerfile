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

# Start the ASGI server
# 
# CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "ecommerce.asgi:application", "--access-log", "-", "--proxy-headers"]
CMD ["gunicorn", "ecommerce.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--workers", "3", "--timeout", "300"]