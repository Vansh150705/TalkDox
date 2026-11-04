FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data TALKDOX_ENV=production PORT=7860

# Optional system packages: tesseract = local OCR (no page images leave the server),
# libreoffice-core = legacy .doc/.ppt/.xls/.rtf/.odt support. Remove if you don't need them.
ARG WITH_OCR=1
ARG WITH_LIBREOFFICE=0
RUN if [ "$WITH_OCR" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*; fi \
 && if [ "$WITH_LIBREOFFICE" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress && rm -rf /var/lib/apt/lists/*; fi

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Optional JS rendering:  docker build --build-arg WITH_JS=1 ...
ARG WITH_JS=0
COPY requirements-js.txt .
RUN if [ "$WITH_JS" = "1" ]; then pip install -r requirements-js.txt && playwright install --with-deps chromium; fi

COPY app ./app
COPY main.py .

RUN useradd --create-home --uid 1000 talkdox && mkdir -p /data && chown -R talkdox /data /app
USER talkdox
VOLUME /data
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','7860')+'/api/health',timeout=4)" || exit 1
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
