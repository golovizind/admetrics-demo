#!/usr/bin/env python3
"""Почему бот не разговаривает с Telegram.

Запускать НА СЕРВЕРЕ:
    cd /opt/admetrics && set -a && . ./.env && set +a && .venv/bin/python check_telegram.py

Токен нигде не печатается — ни в выводе, ни в сообщениях об ошибках.
Проверяет по порядку: прямую связь, связь через TELEGRAM_PROXY, и принимает
ли Telegram сам токен. Первая же неудача называет причину.
"""

from __future__ import annotations

import os
import re
import sys

try:
    import requests
except ImportError:
    sys.exit("Нет requests — запускайте через .venv/bin/python")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
OK, BAD = "  OK  ", " НЕТ  "


def mask(text: object) -> str:
    out = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot<скрыт>", str(text))
    return out.replace(TOKEN, "<скрыт>") if TOKEN else out


def say(good: bool, what: str, detail: str = "") -> bool:
    print(f"[{OK if good else BAD}] {what}" + (f" — {detail}" if detail else ""))
    return good


def probe(proxies: dict | None) -> tuple[bool, str]:
    """(достучались, что случилось). Бьём в getMe — он же и проверит токен."""
    try:
        r = requests.post(f"{API}/getMe", timeout=20, proxies=proxies)
    except requests.RequestException as exc:
        return False, mask(exc)
    try:
        data = r.json()
    except ValueError:
        return False, f"ответ не JSON, код {r.status_code}"
    if data.get("ok"):
        return True, "@" + str(data["result"].get("username", "?"))
    return False, f"Telegram отказал: {data.get('error_code')} " \
                  f"{mask(data.get('description'))}"


if not say(bool(TOKEN), "TELEGRAM_BOT_TOKEN задан",
           "" if TOKEN else "переменная пуста — подгрузите .env"):
    raise SystemExit(1)

# 1. Напрямую -------------------------------------------------------------- #
direct_ok, direct_why = probe(None)
say(direct_ok, "прямая связь с api.telegram.org", direct_why)

# 2. Через прокси ---------------------------------------------------------- #
if PROXY:
    scheme = PROXY.split("://", 1)[0].lower()
    if scheme.startswith("socks"):
        try:
            import socks  # noqa: F401  (PySocks)
        except ImportError:
            say(False, "PySocks установлен",
                "socks5-прокси без него не работает: pip install PySocks")
            raise SystemExit(1)
    # Хост прокси печатаем без логина и пароля.
    shown = re.sub(r"://[^@/]*@", "://<логин скрыт>@", PROXY)
    proxy_ok, proxy_why = probe({"http": PROXY, "https": PROXY})
    say(proxy_ok, f"связь через прокси ({shown})", proxy_why)
    alive = proxy_ok
else:
    say(direct_ok, "TELEGRAM_PROXY не задан",
        "" if direct_ok else "прямая связь не работает — нужен прокси")
    alive = direct_ok

# 3. Вебхук: он молча забирает обновления у long polling ------------------- #
if alive:
    try:
        info = requests.post(f"{API}/getWebhookInfo", timeout=20,
                             proxies={"http": PROXY, "https": PROXY} if PROXY
                             else None).json()
        url = (info.get("result") or {}).get("url") or ""
        say(not url, "вебхук не мешает long polling",
            f"стоит вебхук {url} — обновления уходят туда, бот их не увидит"
            if url else "")
    except requests.RequestException as exc:
        say(False, "проверка вебхука", mask(exc))

print()
if alive:
    print("Связь есть. Если бот всё равно молчит — смотрите "
          "journalctl -u admetrics-bot -n 50")
    raise SystemExit(0)

print("Связи с Telegram нет.")
if not PROXY:
    print("Пропишите прокси в /opt/admetrics/.env одной строкой:")
    print("  TELEGRAM_PROXY=socks5://пользователь:пароль@хост:порт")
    print("и перезапустите: systemctl restart admetrics-bot")
else:
    print("Прокси задан, но через него не проходит. Проверьте его отдельно:")
    print(f"  curl -x '{re.sub(r'://[^@/]*@', '://<логин>:<пароль>@', PROXY)}'"
          " -s -o /dev/null -w '%{http_code}\\n' https://api.telegram.org/")
print()
print("Сбор статистики при этом идёт — он ходит в Meta через прокси личек, "
      "и Telegram ему не нужен.")
raise SystemExit(1)
