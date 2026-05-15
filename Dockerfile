FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN useradd -m -u 10001 botuser && \
    mkdir -p /app/projects /app/data/sessions /app/data/history /app/logs /app/tmp && \
    chown -R botuser:botuser /app

USER botuser

CMD ["python", "bot.py"]
