#!/usr/bin/env bash
# Демо Admetrics одной командой: зависимости, ключ, вымышленные данные, панель.
#   ./run_demo.sh            → http://127.0.0.1:8080
#   PORT=9000 ./run_demo.sh
#   RESEED=0 ./run_demo.sh   → не пересоздавать базу
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8080}"

if [[ ! -x .venv/bin/python ]]; then
  echo "==> Ставлю зависимости в .venv"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [[ ! -f .env ]]; then
  echo "==> Создаю .env с демо-ключом шифрования"
  KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  cat > .env <<EOF
ADMETRICS_KEY=$KEY
ADMETRICS_BASE_CURRENCY=RUB
# Фиктивный токен: нужен панели только как ключ подписи, в Telegram не ходит.
TELEGRAM_BOT_TOKEN=000000000:DEMO-not-a-real-token
EOF
fi
set -a; . ./.env; set +a

if [[ "${RESEED:-1}" == "1" || ! -f admetrics.sqlite3 ]]; then
  echo "==> Генерирую демо-данные"
  .venv/bin/python demo_seed.py
fi

# Права админа для браузера на этой же машине — только для локального демо.
export ADMETRICS_LOCAL_ADMIN=1
echo "==> Панель: http://127.0.0.1:$PORT"
exec .venv/bin/python -u webapp.py --port "$PORT"
