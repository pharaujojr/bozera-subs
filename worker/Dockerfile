FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir subliminal==2.8.0 requests==2.34.2

COPY worker/worker.py shared/config.py ./

CMD ["python", "-u", "worker.py"]
