# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Tesseract for the OCR fallback; fonts/libs for reportlab and Chromium.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng \
        fonts-dejavu-core libffi-dev shared-mime-info curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-local-ai.txt ./

# Chromium is installed into the image by default. Build with --build-arg INSTALL_BROWSERS=0 only on a
# build host that cannot reach the Playwright CDN; mount a browser at PLAYWRIGHT_EXECUTABLE_PATH instead.
ARG INSTALL_BROWSERS=1

# Optional corporate/chambers proxy CA (TLS-inspecting proxies):
#   docker build --secret id=extra_ca,src=/path/to/proxy-ca.crt -t corpus-service:local .
# The secret is never written into an image layer.
RUN --mount=type=secret,id=extra_ca,target=/run/secrets/extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then \
        cp /run/secrets/extra_ca /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates; \
    fi \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_BROWSERS" = "1" ]; then playwright install --with-deps chromium; fi

# Optional local/private AI engine library (build with --build-arg LOCAL_AI=1).
ARG LOCAL_AI=0
RUN --mount=type=secret,id=extra_ca,target=/run/secrets/extra_ca,required=false \
    if [ "$LOCAL_AI" = "1" ]; then pip install --no-cache-dir -r requirements-local-ai.txt; fi

COPY . .
COPY certs/RapidSSL_TLS_RSA_CA_G1.pem /usr/local/share/ca-certificates/rapidssl-tls-rsa-ca-g1.crt
RUN update-ca-certificates \
    && mkdir -p /app/live /app/raw /app/state \
    && python -m compileall -q scraper migrations

EXPOSE 8000
CMD ["uvicorn", "scraper.main:app", "--host", "0.0.0.0", "--port", "8000"]
