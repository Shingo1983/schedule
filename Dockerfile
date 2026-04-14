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

# Playwright のブラウザ配置先を固定（既定の $HOME/.cache ではなく /ms-playwright）
# 実行時ユーザが root 以外になっても同じ場所を参照できるよう明示
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Chromium のダウンロードが失敗しても pip install は成功してしまうので、
# 別 RUN に分けて、インストール直後に実体バイナリの存在を検証する。
# 見つからなければビルドを即失敗させ、サイレント失敗のまま本番へ流れるのを防ぐ。
RUN python -m playwright install --with-deps chromium \
 && ls -la "$PLAYWRIGHT_BROWSERS_PATH" 2>&1 \
 && find "$PLAYWRIGHT_BROWSERS_PATH" -maxdepth 4 \( -name 'chrome' -o -name 'headless_shell' \) -print 2>&1 \
 && if [ -z "$(find "$PLAYWRIGHT_BROWSERS_PATH" -maxdepth 4 \( -name 'chrome' -o -name 'headless_shell' \) 2>/dev/null)" ]; then \
        echo "!!! Playwright Chromium binary not found after install !!!" && exit 1; \
    fi

COPY . .
RUN chmod +x /app/start.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Railway Hobby(512MB-1GB) では Chromium と並行してワーカー複数は OOM 危険なので 1 に
# gthread で同時リクエスト処理は確保しつつメモリは節約
# start.sh で診断ログを出してから gunicorn を起動する
CMD ["/app/start.sh"]
