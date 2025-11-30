FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Only tiny runtime libs needed (optional but nice)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY . .

# COPY entrypoint.sh /entrypoint.sh
# RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# ENTRYPOINT ["/entrypoint.sh"]
# CMD ["gunicorn", "ecommerce.wsgi:application", "--bind", "0.0.0.0:8000"]
