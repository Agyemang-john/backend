FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# ←←← FIX THE LINE ENDINGS HERE (this is the key line)
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY . .

# Use executable ENTRYPOINT (not string form) + real CMD
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "ecommerce.asgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--timeout", "120", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "50"]