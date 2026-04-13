#!/bin/sh
# Startup shim for Railway - prints diagnostic markers before launching gunicorn.
# This helps diagnose silent container failures when Deploy Logs appear empty.

set -e

echo "=== CONTAINER START ==="
echo "PORT=${PORT:-<unset>}"
echo "PWD=$(pwd)"
echo "PYTHON=$(python --version 2>&1)"
echo "GUNICORN=$(gunicorn --version 2>&1 || echo 'gunicorn not found')"
echo "Files in /app:"
ls -1 /app | head -20
echo "=== Testing wsgi import ==="
python -u -c "from jal_shopping_tool.app import create_app; print('import OK'); create_app(); print('create_app OK')" || {
  echo "!!! WSGI import failed !!!"
  exit 1
}
echo "=== Launching gunicorn on port ${PORT:-8080} ==="
exec gunicorn wsgi:app \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers 1 \
  --threads 4 \
  --worker-class gthread \
  --timeout 300 \
  --log-level info \
  --access-logfile - \
  --error-logfile -
