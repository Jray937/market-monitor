FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

#  tzdata 供 APScheduler UTC 計算
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/app/config.yaml
ENV STATE_FILE=/app/alert_state.json

CMD ["python3", "bot.py"]
