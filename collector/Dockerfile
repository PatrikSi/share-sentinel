FROM python:3.14.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade --no-install-recommends --yes && \
    DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --yes nfs-common && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/share-sentinel

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==26.1.2 setuptools==83.0.0 wheel==0.47.0 && \
    pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 collector && \
    useradd --uid 10001 --gid collector --create-home --home-dir /home/collector collector && \
    mkdir -p /data && chown collector:collector /data

ENV TMPDIR=/data

COPY --chown=collector:collector collector_entrypoint.py share_sentinel_collector.py share_sentinel_sharepoint.py ./
COPY --chown=collector:collector sharepoint ./sharepoint

USER collector
WORKDIR /data

ENTRYPOINT ["python", "/opt/share-sentinel/collector_entrypoint.py"]
CMD ["--help"]
