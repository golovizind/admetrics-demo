#!/usr/bin/env python3
"""Почему в боте нет кнопки «Открыть панель».

Кнопка рисуется, только если выполнены все условия сразу, и до сих пор
невыполненное условие не называлось — кнопка просто отсутствовала.
Этот скрипт проходит те же проверки и печатает, какая именно не прошла.

Запуск:  .venv/bin/python check_webapp.py
"""

from __future__ import annotations

import os
import socket
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK, BAD = "  OK  ", " НЕТ  "
fails: list[str] = []


def say(good: bool, what: str, detail: str = "") -> bool:
    print(f"[{OK if good else BAD}] {what}" + (f" — {detail}" if detail else ""))
    if not good:
        fails.append(what)
    return good


# 1. Панель слушает порт локально ------------------------------------------- #
port = int(os.environ.get("PORT", "8080"))
sock = socket.socket()
sock.settimeout(2)
listening = sock.connect_ex(("127.0.0.1", port)) == 0
sock.close()
say(listening, f"панель слушает 127.0.0.1:{port}",
    "" if listening else "не запущена — смотрите строки [панель] в терминале")

# 2. Адрес туннеля записан --------------------------------------------------- #
path = os.path.join(HERE, ".webapp_url")
url = (os.environ.get("WEBAPP_URL", "").strip()
       or (open(path, encoding="utf-8").read().strip()
           if os.path.exists(path) else ""))
src = "переменная WEBAPP_URL" if os.environ.get("WEBAPP_URL", "").strip() \
    else ".webapp_url"
if not say(bool(url), "адрес панели известен", f"{url or 'пусто'} ({src})"):
    print("\nИтог: туннель не поднялся. Запускайте с TUNNEL=1 ./run.sh")
    raise SystemExit(1)

# 3. Адрес https — Telegram другого не принимает ----------------------------- #
say(url.startswith("https://"), "адрес по https",
    "" if url.startswith("https://") else "Telegram отвергнет кнопку целиком")

# 4. Тот же ответ, что видит бот --------------------------------------------- #
req = urllib.request.Request(
    url.rstrip("/") + "/api/summary",
    headers={"User-Agent": "Mozilla/5.0 (Admetrics liveness probe)",
             "Accept": "application/json"})
alive, why = False, ""
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        body = resp.read(200)
        alive = "json" in ctype
        why = f"{resp.status}, Content-Type {ctype or '-'}"
        if not alive:
            why += f"\n        начало ответа: {body[:120]!r}"
except urllib.error.HTTPError as exc:
    alive = exc.code in (401, 403)
    why = f"{exc.code}" + (" — панель требует вход, это норма" if alive else "")
except Exception as exc:  # noqa: BLE001
    why = f"{type(exc).__name__}: {exc}"
say(alive, "панель отвечает через туннель", why)

# 5. Права локального админа ------------------------------------------------- #
flag = os.environ.get("ADMETRICS_LOCAL_ADMIN", "").strip()
tun = os.environ.get("TUNNEL", "0").strip()
if tun == "1" and flag == "1":
    say(False, "локальный админ выключен при туннеле",
        "ADMETRICS_LOCAL_ADMIN=1 вместе с туннелем открывает панель всему "
        "интернету правами админа")
else:
    say(True, "режим доступа согласован",
        f"TUNNEL={tun or '0'}, ADMETRICS_LOCAL_ADMIN={flag or '0'}")

print()
if fails:
    print("Кнопки нет из-за: " + "; ".join(fails))
    raise SystemExit(1)
print("Все проверки прошли — кнопка должна быть. Если её не видно, откройте "
      "любой экран бота заново: клавиатура рисуется в момент отправки.")
