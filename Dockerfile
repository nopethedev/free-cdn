FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CASCADE_DATA_DIR=/data \
    CASCADE_HOST=0.0.0.0 \
    CASCADE_PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir yt-dlp

COPY cascade ./cascade

RUN useradd --create-home --uid 10001 cascade \
    && mkdir -p /data \
    && chown -R cascade:cascade /data /app
USER cascade

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "cascade"]
