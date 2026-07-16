FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home --home-dir /home/app app && \
    mkdir -p /artifacts && chown app:app /artifacts

COPY --chown=app:app . .

USER app

CMD ["python", "-m", "worker.main"]
