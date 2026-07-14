FROM python:3.12-slim

RUN pip install --no-cache-dir docker==7.1.0 prometheus_client==0.20.0

WORKDIR /app
COPY docker/exporter.py ./

ENV POLL_INTERVAL=15
ENV METRICS_PORT=9358

EXPOSE 9358

CMD ["python", "exporter.py"]
