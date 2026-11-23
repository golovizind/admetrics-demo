#!/usr/bin/env python3
"""Почему бот молчит. Один прогон — один ответ.

Проверяет по порядку то, на чём бот обычно и останавливается: настройки,
связь с Telegram, чужой процесс на том же токене. Секретов не печатает:
ни токена, ни ключей, ни логина прокси — вывод можно пересылать как есть.

    cd ~/Downloads/admetrics && set -a && . ./.env && set +a && python3 check_bot.py
"""
from __future__ import annotations

import os
import sys
import urllib.parse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OK, BAD, WARN = "  ok  ", " ОШИБКА ", "  !!  "


def say(mark: str, text: str) -> None:
    print(f"[{mark}] {text}")


def hide(proxy: str) -> str:
    """Прокси без логина и пароля.

    Скрыты НАМЕРЕННО, чтобы вывод можно было переслать целиком: в .env они
    лежат как есть. Короткая строка здесь — не признак того, что что-то
    потерялось.
    """
    if not proxy:
        return "не задан"
    try:
        u = urllib.parse.urlparse(proxy)
        return f"{u.scheme}://{u.hostname}:{u.port or ''}"
    except ValueError:
        return "задан"


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    portal = os.environ.get("WEBAPP_URL", "").strip()
    admins = os.environ.get("ADMIN_TG_IDS", "").strip()
    raw_proxy = os.environ.get("TELEGRAM_PROXY", "").strip()
    proxy = raw_proxy
    bad = 0

    print("НАСТРОЙКИ")
    if token:
        say(OK, f"TELEGRAM_BOT_TOKEN задан ({len(token)} символов)")
    else:
        say(BAD, "TELEGRAM_BOT_TOKEN не задан — бот не запустится"); bad += 1
    if portal.startswith("http"):
        say(OK, f"WEBAPP_URL = {portal}")
    else:
        say(WARN, "WEBAPP_URL не задан — бот ответит, но без кнопки на портал")
    say(OK if admins else WARN,
        f"ADMIN_TG_IDS = {admins or 'пусто — уведомления некому слать'}")
    if raw_proxy and proxy != raw_proxy:
        say(WARN, f"TELEGRAM_PROXY = {hide(proxy)} — схему поправил на "
                  f"{proxy.split('://')[0]}://, чтобы имя разрешал прокси")
    else:
        say(OK if proxy else WARN, f"TELEGRAM_PROXY = {hide(proxy)}"
            + (" (логин и пароль скрыты, в .env они на месте)" if proxy else ""))

    if not token:
        return 1

    print("\nСВЯЗЬ С TELEGRAM")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        # Через резидентный прокси первый ответ бывает медленным: 25 секунд
        # мало, и «таймаут» здесь означал бы не поломку, а нетерпение.
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe",
                         timeout=45, proxies=proxies)
        data = r.json()
    except requests.RequestException as exc:
        # Текст исключения содержит URL, а в нём — токен: чистим.
        text = str(exc).replace(token, "<скрыт>")
        say(BAD, f"не достучались: {text[:200]}")
        if "UNEXPECTED_EOF" in text or "EOF occurred" in text:
            say(WARN, "туннель прокси оборвался на полуслове. Так ведут себя "
                      "резидентные прокси, когда по соединению долго нет "
                      "данных. Для long polling это лечится коротким окном: "
                      "TELEGRAM_POLL_SECONDS=15 в .env")
        elif "Read timed out" in text and proxy:
            say(WARN, "прокси принял нас, но ответа от Telegram не дождался. "
                      "Проверьте, что он выпускает трафик наружу: "
                      "curl -s --max-time 40 -x \"$TELEGRAM_PROXY\" "
                      "https://ifconfig.me")
        elif "Name or service not known" in text:
            say(WARN, "имя api.telegram.org не разрешается. Если прокси задан, "
                      "схема должна быть socks5h:// — с socks5:// имя резолвит "
                      "эта машина, а не прокси")
        elif not proxy:
            say(WARN, "прокси не задан. Если Telegram режет провайдер, добавьте "
                      "в .env: TELEGRAM_PROXY=socks5h://логин:пароль@хост:порт")
        return 1
    if not data.get("ok"):
        say(BAD, f"Telegram отказал: {data.get('error_code')} "
                 f"{data.get('description')}")
        if data.get("error_code") == 401:
            say(WARN, "токен перевыпустили — впишите новый в .env")
        return 1
    me = data["result"]
    say(OK, f"Telegram отвечает: @{me.get('username')}")

    print("\nКТО ЗАБИРАЕТ СООБЩЕНИЯ")
    try:
        hook = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo",
                            timeout=25, proxies=proxies).json().get("result", {})
    except requests.RequestException:
        hook = {}
    if hook.get("url"):
        # Пока висит вебхук, getUpdates возвращает пустоту, и бот молчит
        # при полностью исправном токене и связи. Ловится только так.
        say(BAD, f"на токене висит вебхук: {hook['url']}")
        say(WARN, "пока он есть, long polling не получает НИЧЕГО. Снять: "
                  f"curl \"https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN"
                  f"/deleteWebhook\"")
        bad += 1
    else:
        say(OK, "вебхука нет — long polling получает обновления")
    if hook.get("pending_update_count"):
        say(WARN, f"необработанных сообщений: {hook['pending_update_count']} — "
                  "их никто не забирает")

    try:
        upd = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                           params={"timeout": 0, "limit": 1},
                           timeout=25, proxies=proxies).json()
    except requests.RequestException as exc:
        say(BAD, str(exc).replace(token, "<скрыт>")[:150]); return 1
    if upd.get("error_code") == 409:
        # Самая обидная поломка: всё исправно, но сообщения забирает
        # забытая копия, и отвечает она старым кодом.
        say(BAD, "409: этот токен уже опрашивает ДРУГОЙ процесс")
        say(WARN, "найдите его и остановите: ps aux | grep -i 'bot.py'")
        bad += 1
    elif upd.get("ok"):
        say(OK, "getUpdates работает — сообщения дойдут до бота")

    print()
    if bad:
        print("Итог: бот молчит по причине выше.")
        return 1
    print("Итог: со стороны Telegram всё исправно. Если бот всё равно молчит —"
          "\nзначит процесс не запущен. Проверьте: ps aux | grep bot.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
