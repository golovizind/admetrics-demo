#!/usr/bin/env bash
# Развёртывание Admetrics на сервере. Запускать НА СЕРВЕРЕ от root:
#
#     DOMAIN=admetrics.example.com bash /opt/admetrics/deploy.sh
#
# Без DOMAIN панель поднимется, но останется доступна только с самого
# сервера: Telegram пускает Mini App лишь по валидному HTTPS, а на голом
# IP сертификата не бывает.
#
# Идемпотентно: гоняется повторно после каждого обновления кода.

set -euo pipefail

APP_DIR=/opt/admetrics
APP_USER=admetrics
PORT="${PORT:-8080}"
DOMAIN="${DOMAIN:-}"

[[ $EUID -eq 0 ]] || { echo "Запускать от root"; exit 1; }
[[ -f "$APP_DIR/bot.py" ]] || { echo "Нет $APP_DIR/bot.py — сначала скопируйте проект"; exit 1; }
[[ -f "$APP_DIR/.env"   ]] || { echo "Нет $APP_DIR/.env — без него нечем работать"; exit 1; }

# На свежем сервере первым делом просыпается unattended-upgrades и держит
# замок dpkg. Ждём его, а не падаем: через пару минут он сам закончит.
wait_apt() {
  local waited=0
  while fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock \
              /var/cache/apt/archives/lock >/dev/null 2>&1; do
    if (( waited == 0 )); then
      echo "    apt занят автообновлением системы — жду (до 10 минут)"
    fi
    sleep 5; waited=$((waited + 5))
    if (( waited >= 600 )); then
      echo "    замок apt не отпустили за 10 минут. Посмотрите, что держит:"
      echo "      ps aux | grep -E 'apt|dpkg|unattended'"
      exit 1
    fi
  done
  (( waited > 0 )) && echo "    замок отпущен через ${waited} с"
  return 0
}

echo "==> Зависимости системы"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  command -v fuser >/dev/null || apt-get install -y -qq psmisc >/dev/null 2>&1 || true
  wait_apt
  apt-get update -qq
  wait_apt
  apt-get install -y -qq python3 python3-venv python3-pip sqlite3 ca-certificates tzdata curl
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pip sqlite ca-certificates tzdata curl
else
  echo "Ни apt, ни dnf — поставьте python3, python3-venv, sqlite3 вручную"; exit 1
fi

echo "==> Пользователь $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 \
  || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Виртуальное окружение"
# Всегда пересобирается на месте: в .venv с Mac лежат бинарники под macOS.
rm -rf "$APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Настройки окружения"
# Локальный админ на сервере должен быть выключен наглухо. Обратный прокси
# подключается с 127.0.0.1, то есть по адресу соединения он неотличим от
# браузера на самой машине — с флагом=1 панель открылась бы всему интернету
# правами админа. (В коде есть вторая защита по заголовку Host, но полагаться
# на одну проверку там, где цена ошибки — весь доступ, неправильно.)
sed -i '/^ADMETRICS_LOCAL_ADMIN=/d' "$APP_DIR/.env"
echo 'ADMETRICS_LOCAL_ADMIN=0' >> "$APP_DIR/.env"

if [[ -n "$DOMAIN" ]]; then
  # Имя бота нужно странице входа: без него виджет Telegram не нарисуется,
  # и портал в браузере будет показывать пустую карточку.
  if ! grep -q "^TELEGRAM_BOT_USERNAME=" "$APP_DIR/.env"; then
    echo "    ВНИМАНИЕ: в .env нет TELEGRAM_BOT_USERNAME — вход в портал через"
    echo "    браузер не заработает. Допишите имя бота без @ и повторите."
  fi
  # Постоянный адрес панели. Раньше он приезжал из .webapp_url, который писал
  # туннель, и менялся на каждом подъёме — кнопка Mini App жила до первого
  # обрыва. Теперь адрес зашит и не меняется никогда.
  sed -i '/^WEBAPP_URL=/d' "$APP_DIR/.env"
  echo "WEBAPP_URL=https://$DOMAIN" >> "$APP_DIR/.env"
fi

echo "==> Права"
mkdir -p "$APP_DIR/backups"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# В .env ключ шифрования мультитокенов, токен бота и ключ MyBroCards.
chmod 600 "$APP_DIR/.env"
chmod 700 "$APP_DIR/backups"

echo "==> systemd: бот"
cat > /etc/systemd/system/admetrics-bot.service <<UNIT
[Unit]
Description=Admetrics Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -u bot.py
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes

[Install]
WantedBy=multi-user.target
UNIT

echo "==> systemd: панель"
cat > /etc/systemd/system/admetrics-panel.service <<UNIT
[Unit]
Description=Admetrics web panel
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
# Слушает только 127.0.0.1 — наружу её выставляет Caddy с сертификатом.
ExecStart=$APP_DIR/.venv/bin/python -u webapp.py --port $PORT
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes

[Install]
WantedBy=multi-user.target
UNIT

echo "==> systemd: воркер Telegram-аккаунтов"
# Держит MTProto-сессии технических аккаунтов: коды, здоровье, каналы,
# публикации. Без TG_API_ID/TG_API_HASH в .env он просто ждёт и ничего не делает.
cat > /etc/systemd/system/admetrics-tg.service <<UNIT
[Unit]
Description=Admetrics Telegram accounts worker
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -u tg_worker.py
Restart=always
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes

[Install]
WantedBy=multi-user.target
UNIT

echo "==> systemd: ежедневный бэкап базы"
cat > /etc/systemd/system/admetrics-backup.service <<UNIT
[Unit]
Description=Admetrics database backup

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
# Именно .backup, а не cp: копирование файла под работающей записью даёт
# битую базу. .backup делает согласованный снимок.
ExecStart=/bin/bash -c 'sqlite3 $APP_DIR/admetrics.sqlite3 ".backup $APP_DIR/backups/db-\$(date +%%Y%%m%%d).sqlite3" && ls -1t $APP_DIR/backups/db-*.sqlite3 | tail -n +8 | xargs -r rm --'
UNIT

cat > /etc/systemd/system/admetrics-backup.timer <<UNIT
[Unit]
Description=Admetrics database backup daily

[Timer]
OnCalendar=daily
Persistent=yes

[Install]
WantedBy=timers.target
UNIT

if [[ -n "$DOMAIN" ]]; then
  echo "==> Caddy на $DOMAIN"
  if ! command -v caddy >/dev/null; then
    wait_apt
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
      > /etc/apt/sources.list.d/caddy-stable.list
    wait_apt
    apt-get update -qq
    apt-get install -y -qq caddy
  fi
  # Caddy сам получает сертификат Let's Encrypt и сам его продлевает.
  # Ничего с ключами руками делать не надо, и просрочить их нельзя.
  cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	reverse_proxy 127.0.0.1:$PORT
	encode gzip
	# Панель живёт внутри Telegram. Встраивать её куда-то ещё незачем,
	# а вот кликджекинг на кнопках «сменить владельца» — вполне реален.
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "no-referrer"
	}
}
CADDY
  systemctl enable --now caddy
  systemctl reload caddy || systemctl restart caddy

  # Трогаем фаервол только если он уже включён: команда ufw enable на машине
  # без разрешённого 22-го порта отрезает вас от собственного сервера.
  if command -v ufw >/dev/null && ufw status | grep -q "^Status: active"; then
    ufw allow 80/tcp  >/dev/null
    ufw allow 443/tcp >/dev/null
    echo "    ufw: открыты 80 и 443"
  fi
fi

echo "==> Запуск"
systemctl daemon-reload
systemctl enable --now admetrics-bot.service admetrics-panel.service admetrics-tg.service admetrics-backup.timer
systemctl restart admetrics-bot.service admetrics-panel.service admetrics-tg.service
sleep 4

echo
echo "==================== ГОТОВО ===================="
for unit in admetrics-bot admetrics-panel admetrics-tg; do
  printf '%-18s %s\n' "$unit" "$(systemctl is-active $unit)"
done
[[ -n "$DOMAIN" ]] && printf '%-18s %s\n' "caddy" "$(systemctl is-active caddy)"
echo
if [[ -n "$DOMAIN" ]]; then
  echo "Проверка панели снаружи:"
  # Сертификат Let's Encrypt берётся 30-90 секунд после старта Caddy, поэтому
  # одна проверка сразу после запуска почти всегда показывает «не ответила»
  # на полностью исправной установке. Ждём до полутора минут.
  code=000
  for _ in $(seq 1 9); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
             "https://$DOMAIN/api/summary" 2>/dev/null) || code=000
    [[ "$code" != "000" ]] && break
    sleep 10
  done
  case "$code" in
    401) echo "  https://$DOMAIN — ОК: отвечает 401, то есть требует вход из Telegram." ;;
    000) echo "  https://$DOMAIN — не ответила. Сертификат берётся минуту-две;"
         echo "  если и потом молчит — проверьте, что A-запись домена смотрит на этот сервер"
         echo "  и что 80/443 не закрыты фаерволом провайдера." ;;
    *)   echo "  https://$DOMAIN — ответила $code (ожидался 401). Смотрите journalctl -u caddy." ;;
  esac
else
  echo "DOMAIN не задан — панель слушает только 127.0.0.1:$PORT и снаружи недоступна."
  echo "Кнопки Mini App в боте не будет, пока не появится домен."
fi
echo
echo "Логи бота:    journalctl -u admetrics-bot -f"
echo "Логи панели:  journalctl -u admetrics-panel -f"
echo "Логи TG:      journalctl -u admetrics-tg -f"
echo "Перезапуск:   systemctl restart admetrics-bot"
