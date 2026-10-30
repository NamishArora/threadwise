FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY threadwise ./threadwise
RUN adduser --disabled-password --gecos "" --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /app /data
USER app
EXPOSE 8080
CMD ["uvicorn", "threadwise.api:app", "--host", "0.0.0.0", "--port", "8080"]
