FROM python:3.14.6-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install --no-install-recommends --yes nfs-common && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/share-sentinel

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==26.1.2 setuptools==83.0.0 wheel==0.47.0 && \
    pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 collector && \
    useradd --uid 10001 --gid collector --create-home --home-dir /home/collector collector && \
    mkdir -p /data && chown collector:collector /data

COPY --chown=collector:collector share_sentinel_collector.py .

USER collector
WORKDIR /data

ENTRYPOINT ["python", "/opt/share-sentinel/share_sentinel_collector.py"]
CMD ["--help"]
