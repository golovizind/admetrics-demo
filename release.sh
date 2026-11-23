#!/usr/bin/env bash
# Выкатка Admetrics на сервер. Запускать НА MAC из папки проекта:
#
#     ./release.sh "что поменяли"
#
# Что делает: коммитит изменения в локальный git, отправляет ровно этот
# коммит на сервер архивом и перезапускает службы.
#
# В GitLab не пушит — решение владельца. Локальный коммит остаётся: по нему
# видно, что выкачено (сервер хранит хеш в .deployed-commit), и откат
# делается одной командой. Отправить в GitLab вручную, если понадобится:
#     git push origin main
#
# Прогонять можно сколько угодно раз.

set -euo pipefail
cd "$(dirname "$0")"

SERVER="${SERVER:-root@203.0.113.10}"
APP_DIR=/opt/admetrics
BRANCH="${BRANCH:-main}"
MSG="${1:-}"

DOMAIN_FILE=".domain"
DOMAIN="${DOMAIN:-}"
if [[ -z "$DOMAIN" && -f "$DOMAIN_FILE" ]]; then
  DOMAIN="$(cat "$DOMAIN_FILE")"
fi
[[ -n "$DOMAIN" ]] && printf '%s' "$DOMAIN" > "$DOMAIN_FILE"
[[ -n "$DOMAIN" ]] || { echo "Нет домена. Разово: DOMAIN=admetrics.example.com ./release.sh '…'"; exit 1; }

[[ -f bot.py ]] || { echo "Запускать из папки проекта"; exit 1; }
[[ -d .git   ]] || { echo "Это не git-репозиторий. Сначала: git init"; exit 1; }

# ── 1. Локальный коммит ───────────────────────────────────────────────────
if [[ -n "$(git status --porcelain)" ]]; then
  echo "==> Изменения:"
  git status --short | sed 's/^/    /'
  # .env, база и бэкапы отсечены .gitignore. Если что-то из них всплыло в
  # списке выше — значит .gitignore сломан, и это надо чинить, а не выкатывать.
  if git status --porcelain | grep -Eq '(^|/)(\.env|\.domain|.*\.sqlite3)'; then
    echo
    echo "СТОП: в изменениях секреты или база. Проверьте .gitignore, ничего не выкачено."
    exit 1
  fi
  [[ -n "$MSG" ]] || { echo; echo "Укажите, что поменяли: ./release.sh 'правка отчётов'"; exit 1; }
  git add -A
  git -c user.email="dev@example.com" -c user.name="Danil" commit -q -m "$MSG"
  echo "    коммит сделан"
else
  echo "==> Новых изменений нет — выкачу текущий коммит"
fi

SHA="$(git rev-parse HEAD)"

# Сервер иногда рвёт SSH ещё до входа («Connection closed by … port 22»):
# так sshd сбрасывает лишние соединения, когда его одновременно сканируют
# боты (MaxStartups). Это не про нас и проходит со второй-третьей попытки,
# поэтому каждый шаг по SSH повторяется. Оба шага безопасно повторять:
# распаковка архива и deploy.sh идемпотентны.
retry() {
  local try
  for try in 1 2 3 4 5; do
    if "$@"; then return 0; fi
    echo "    попытка $try не прошла, жду и повторяю…"
    sleep $((try * 3))
  done
  echo "    SSH так и не пустил. Проверьте: ssh $SERVER"
  return 1
}

ship() {
  git archive --format=tar "$SHA" | ssh -o ConnectTimeout=20 "$SERVER" "
    set -e
    mkdir -p '$APP_DIR'
    tar xf - -C '$APP_DIR'
    printf '%s\\n' '$SHA' > '$APP_DIR/.deployed-commit'
    if [ -d '$APP_DIR/.git' ] && ! git -C '$APP_DIR' rev-parse HEAD >/dev/null 2>&1; then
      rm -rf '$APP_DIR/.git'
    fi
  "
}

# ── 2. Код едет на сервер ─────────────────────────────────────────────────
# git archive, а не tar текущей папки: в архив попадает ровно содержимое
# коммита. Никакого мусора из рабочей копии и никаких расхождений между тем,
# что записано в истории, и тем, что работает на сервере.
echo "==> Отправляю код на сервер"
retry ship
echo "    доставлен ${SHA:0:8}"

# ── 3. Развёртывание ──────────────────────────────────────────────────────
echo "==> Разворачиваю"
retry ssh -o ConnectTimeout=20 "$SERVER" "DOMAIN='$DOMAIN' PORT='${PORT:-8080}' bash $APP_DIR/deploy.sh"

echo
echo "Выкачено: ${SHA:0:8}  ·  https://$DOMAIN"
echo "Откатиться на прошлый коммит: git revert HEAD && ./release.sh 'откат'"
