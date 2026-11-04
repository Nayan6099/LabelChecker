# LabelCheck: one image, two roles
#   web:    uvicorn app.main:app ...          (default CMD)
#   worker: python -m app.worker              (override the command)
FROM python:3.13-slim AS base

# Tesseract OCR (English + Hindi). Fonts are only needed to generate test samples.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
      fonts-dejavu-core fonts-freefont-ttf libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY tools ./tools

# Non-root user: a break-in gets no root inside the container
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown appuser /data
USER appuser

ENV LABELCHECK_DATA=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_THREAD_LIMIT=1
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health/live || exit 1

# --proxy-headers: trust X-Forwarded-* from the proxy in front (Caddy / ALB) for client IPs and https
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "30"]
