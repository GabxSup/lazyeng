# AURUM AI — no runtime Python dependencies beyond the standard library.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AURUM_HOST=0.0.0.0 \
    AURUM_PORT=8080 \
    AURUM_DATA_DIR=/data

WORKDIR /app

RUN groupadd --gid 10001 aurum \
    && useradd --uid 10001 --gid aurum --no-create-home --shell /usr/sbin/nologin aurum \
    && mkdir -p /data \
    && chown -R aurum:aurum /app /data

COPY --chown=aurum:aurum app ./app

USER aurum
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=20s --timeout=5s --start-period=12s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]

CMD ["python", "-m", "app.main"]
