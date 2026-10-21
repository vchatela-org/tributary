# Build: the same Python as the runtime image, plus pip and a shell. Both stages
# are Chainguard's (Wolfi): its free tier publishes `latest` only, so the
# digests are the pins, and Dependabot moves them.
FROM cgr.dev/chainguard/python:latest-dev@sha256:6611f09b165b0ac0ff31c2ae2ac6fc7960483dfc568a7ee4f1bc4a59777287a8 AS build

WORKDIR /app
COPY requirements.txt .
# A venv without pip of its own: the build stage's pip installs into it, so no
# pip (and none of its vendored libraries) reaches the runtime image.
RUN python -m venv --without-pip /app/venv \
    && pip --python /app/venv/bin/python install --no-cache-dir -r requirements.txt \
    && mkdir /app/data

# Run: no shell, no package manager, nothing but Python and the app.
FROM cgr.dev/chainguard/python:latest@sha256:e2ade4461c3ff67acf054214645e0b4e41b99140d7096ed64c1e15f6fe985cdf

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRIBUTARY_DATA_DIR=/app/data \
    PATH=/app/venv/bin:$PATH

WORKDIR /app
COPY --from=build /app/venv /app/venv
# Not root. The data folder (run history and cursors, SQLite) must be
# writable by this uid: Docker volumes inherit it; on Kubernetes set
# securityContext.fsGroup: 10001.
COPY --from=build --chown=10001:10001 /app/data /app/data
COPY app ./app
COPY templates ./templates
COPY static ./static
# The licences travel with the code: Tributary's (MIT), and the Apache 2.0 of
# the files adapted from Whoops, with the NOTICE naming them.
COPY LICENSE NOTICE ./
COPY LICENSES ./LICENSES
USER 10001:10001

# The venv runs on the runtime stage's interpreter: fail the build here, not at
# start, if the two stages ever carry different Pythons, the zones are gone, or
# the AES-GCM binding of `cryptography` does not load.
RUN ["python", "-c", "import fastapi, uvicorn, sqlalchemy, pydantic_core, renpho, zoneinfo; zoneinfo.ZoneInfo('Europe/Paris'); from cryptography.hazmat.primitives.ciphers.aead import AESGCM; AESGCM(bytes(32)).encrypt(bytes(12), b'', b'')"]

EXPOSE 8000

# Exec form: there is no shell to run a command string.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]

# The base image's entrypoint is python itself; reset it so the command is the
# same as before, and overriding it works the same way.
ENTRYPOINT []
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
