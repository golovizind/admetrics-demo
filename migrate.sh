#!/usr/bin/env bash
# Переезд Admetrics с этого Mac на сервер. Запускать НА MAC из папки проекта:
#
#     DOMAIN=admetrics.example.com ./migrate.sh root@203.0.113.10
#
# Что делает: гасит локальные процессы, снимает согласованный снимок базы,
# заливает проект и .env на сервер и запускает там развёртывание.
#
# Прогонять можно сколько угодно раз — это же и способ выкатывать обновления.

set -euo pipefail
cd "$(dirname "$0")"

SERVER="${1:-}"
APP_DIR=/opt/admetrics

# Домен запоминаем. Забыть подставить DOMAIN легко — а последствие тихое и
# скверное: deploy.sh решает, что домена нет, снимает Caddy и панель начинает
# слушать только 127.0.0.1. Снаружи она при этом просто исчезает, хотя обе
# службы бодро рапортуют «active». Один раз указал — дальше берём из файла.
DOMAIN_FILE=".domain"
DOMAIN="${DOMAIN:-}"
if [[ -z "$DOMAIN" && -f "$DOMAIN_FILE" ]]; then
  DOMAIN="$(cat "$DOMAIN_FILE")"
  echo "==> Домен из прошлого запуска: $DOMAIN"
  echo "    сбросить: rm $DOMAIN_FILE   ·   разово другой: DOMAIN=… ./migrate.sh …"
fi
[[ -n "$DOMAIN" ]] && printf '%s' "$DOMAIN" > "$DOMAIN_FILE"

[[ -n "$SERVER" ]] || { echo "Укажите сервер: ./migrate.sh root@203.0.113.10"; exit 1; }
[[ -f .env ]] || { echo "Нет .env — без него на сервере нечем работать"; exit 1; }
[[ -f bot.py ]] || { echo "Запускать из папки проекта"; exit 1; }
[[ -f requirements.txt ]] || { echo "Нет requirements.txt — сервер не соберёт окружение"; exit 1; }

echo "==> Гашу локальные копии"
# Два бота на одном токене дерутся за getUpdates, и отвечает тот, кто успел
# первым. Пока локальный жив, серверный будет отвечать через раз.
for name in bot.py webapp.py tunnel.py; do pkill -f "$name" 2>/dev/null || true; done
sleep 2

# База по умолчанию НЕ едет. Это разные операции: выкатка кода и перенос
# данных. Раньше они были склеены, и каждое обновление кода откатывало
# сервер к снимку с Mac — вместе с привязками Telegram, из-за чего людей
# выкидывало из портала, и вместе со всем, что сервер успел собрать.
# Живёт база теперь там, где работает: на сервере.
SNAP=".migrate-db.sqlite3"
rm -f "$SNAP"
if [[ "${WITH_DB:-0}" == "1" ]]; then
  echo "==> Снимок базы (WITH_DB=1)"
  # .backup, а не cp: копия файла, снятая под работающей записью, бывает
  # битой, и узнаётся это уже на сервере.
  if [[ -f admetrics.sqlite3 ]]; then
    sqlite3 admetrics.sqlite3 ".backup '$SNAP'"
    echo "    $(du -h "$SNAP" | cut -f1), строк платежей: $(sqlite3 "$SNAP" 'select count(*) from card_payments' 2>/dev/null || echo '?')"
    echo "    ВНИМАНИЕ: серверная база будет заменена этой. Всё, что сервер"
    echo "    собрал и все привязки Telegram, сделанные на нём, пропадут."
  else
    echo "    базы нет — сервер начнёт с пустой"
  fi
else
  echo "==> База остаётся на сервере (нетронутой)"
  echo "    перенести с Mac: WITH_DB=1 ./migrate.sh $*"
fi

echo "==> Проверяю связь с сервером"
# Ключ под паролем нужно один раз отдать агенту. Иначе каждая из четырёх
# команд ниже спросит пароль отдельно, а проверка с BatchMode=yes вообще
# получит отказ вместо запроса — выглядит это как «ключа нет», хотя он есть.
if ! ssh-add -l >/dev/null 2>&1; then
  echo "    добавляю ключ в агент — введите пароль от ключа"
  if [[ "$(uname)" == "Darwin" ]]; then
    ssh-add --apple-use-keychain 2>/dev/null || ssh-add
  else
    ssh-add
  fi
fi

if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER" \
     'echo "    сервер: $(hostname), $(. /etc/os-release 2>/dev/null; echo ${PRETTY_NAME:-неизвестно})"'; then
  echo
  echo "Не подключиться. Что это может быть:"
  echo "  · ключ не разложен      → ssh-copy-id $SERVER"
  echo "  · ключ под паролем      → ssh-add --apple-use-keychain ~/.ssh/id_ed25519"
  echo "  · сервер не отвечает    → ssh -v $SERVER  и смотрите, на чём встаёт"
  exit 1
fi

echo "==> Заливаю код"
ssh "$SERVER" "mkdir -p $APP_DIR"
# tar через ssh, а не rsync: rsync должен стоять на обеих сторонах, а на
# минимальном образе сервера его обычно нет.
tar czf - \
  --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  --exclude='.pytest_cache' --exclude='.DS_Store' \
  --exclude='_to_delete' --exclude='_backup-keitaro' --exclude='backups' \
  --exclude='*.sqlite3' --exclude='*.sqlite3-wal' --exclude='*.sqlite3-shm' \
  --exclude='.env' --exclude='.webapp_url' --exclude='.domain' --exclude="$SNAP" \
  . | ssh "$SERVER" "tar xzf - -C $APP_DIR"

echo "==> Заливаю .env"
# .env идёт отдельной командой и сразу с правами 600: в нём ключ шифрования
# мультитокенов, токен бота и ключ MyBroCards.
scp -q .env "$SERVER:$APP_DIR/.env"
ssh "$SERVER" "chmod 600 $APP_DIR/.env"

if [[ -f "$SNAP" ]]; then
  # Базу не перезаписываем вслепую: если на сервере уже собранные данные,
  # затирать их снимком с Mac — потеря. Прежняя отъезжает в сторону с датой.
  scp -q "$SNAP" "$SERVER:$APP_DIR/.incoming-db.sqlite3"
  ssh "$SERVER" "
    set -e
    cd $APP_DIR
    if [ -f admetrics.sqlite3 ]; then
      mv admetrics.sqlite3 admetrics.sqlite3.replaced-\$(date +%Y%m%d-%H%M%S)
      echo '    прежняя база на сервере сохранена рядом с датой в имени'
    fi
    mv .incoming-db.sqlite3 admetrics.sqlite3
  "
  rm -f "$SNAP"
fi

echo "==> Разворачиваю"
ssh "$SERVER" "DOMAIN='$DOMAIN' PORT='${PORT:-8080}' bash $APP_DIR/deploy.sh"

echo
echo "Локальный запуск больше не нужен: бот и панель живут на сервере под"
echo "systemd и поднимаются сами после перезагрузки. Крышку Mac можно закрывать."
