FROM python:3.11-slim

# Playwright Chromium が必要とする最小限のシステムライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 \
        libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
EXPOSE 8080

CMD gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --timeout 300
