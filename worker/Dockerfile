FROM python:3.14.0-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==26.1.2 setuptools==83.0.0 wheel==0.47.0 && \
    pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home --home-dir /home/app app && \
    mkdir -p /artifacts && chown app:app /artifacts

COPY --chown=app:app . .

USER app

CMD ["python", "-m", "worker.main"]
