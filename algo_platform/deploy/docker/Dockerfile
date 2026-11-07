# Container image for running the platform next to other Docker apps (see docs/DEPLOY.md, "Docker").
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Kolkata PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin algo

WORKDIR /app
COPY deploy/requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install -r /tmp/requirements.lock.txt
COPY --chown=algo:algo . /app
RUN mkdir -p /app/data /app/logs && chown algo:algo /app/data /app/logs

USER algo
EXPOSE 8000
STOPSIGNAL SIGINT
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4); sys.exit(0)"
CMD ["python", "run.py"]
