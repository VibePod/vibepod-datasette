FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY metadata.json ./metadata.json
COPY scripts/start-datasette.sh ./start-datasette.sh
RUN chmod +x ./start-datasette.sh

ENV LOGS_DB_PATH=/data/logs.db
ENV PROXY_DB_PATH=/proxy/proxy.db
ENV DATASETTE_HOST=0.0.0.0
ENV DATASETTE_PORT=8001
ENV SQL_TIME_LIMIT_MS=10000
ENV TRUNCATE_CELLS_HTML=80

EXPOSE 8001

CMD ["/app/start-datasette.sh"]
