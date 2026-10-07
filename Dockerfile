FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN useradd --create-home --uid 10001 bot

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
RUN mkdir -p /app/data && chown -R bot:bot /app/data

USER bot
VOLUME ["/app/data"]
# Secrets come from the environment / mounted files at runtime - never baked in.
CMD ["python", "-m", "app.main"]
