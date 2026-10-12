FROM python:3.14-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRIBUTARY_DATA_DIR=/app/data

# Pending Alpine patches (libssl & co. lag behind the base tag)
RUN apk upgrade --no-cache

WORKDIR /app

COPY requirements.txt .
# pip is only needed at build time; its vendored libraries trip image scans
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip

COPY app ./app
COPY templates ./templates
COPY static ./static

# Not root. The data folder (run history and cursors, SQLite) must be
# writable by this uid: Docker volumes inherit it; on Kubernetes set
# securityContext.fsGroup: 10001.
RUN adduser -D -H -u 10001 tributary \
    && mkdir -p /app/data \
    && chown tributary /app/data
USER 10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
