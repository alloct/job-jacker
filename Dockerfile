FROM python:3.12-slim

# Unbuffered output so `docker logs` shows the cycle as it happens.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Non-root, and it owns the state directory so SQLite can write there.
RUN useradd --create-home --uid 10001 watcher \
    && mkdir -p /app/data \
    && chown -R watcher:watcher /app
USER watcher

VOLUME ["/app/data"]

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--config", "/app/config.yaml"]
