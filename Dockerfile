# CyFun Basic Single Pane of Glass: application image.
# Runs as an unprivileged user; the only writable path is the DATA_DIR volume.
# pip and the bundled wheels are removed after installation: the runtime needs neither,
# and their vendored libraries would otherwise show up in vulnerability scans.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get -y --no-install-recommends upgrade \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip* /root/.cache

COPY app/ ./
RUN chown -R root:root /app && chmod -R a-w /app

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "cyfun.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", "--workers", "1"]
