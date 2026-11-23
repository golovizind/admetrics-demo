#!/usr/bin/env python3
"""
Telegram-бот Admetrics — дверь и звонок, больше ничего.

У бота ровно две работы:

  • **впустить.** `/start КОД` привязывает Telegram человека к его профилю
    и отдаёт кнопку на портал. Первый контакт иначе невозможен: у нового
    маркетолога ещё нет ни одного сообщения от нас.
  • **позвать.** Когда отвалился доступ, кабинет забанили или сменилась
    карта — прислать уведомление тому, кого это касается.

Раньше здесь жил весь интерфейс: два десятка экранов, навигация кнопками,
ввод токенов сообщением. Всё это переехало в портал и удалено отсюда —
не потому, что работало плохо, а потому, что существовало в двух местах
сразу. Две реализации одного и того же расходятся в первый же день, и
расхождение замечают не сразу: человек видит в боте одно, в портале другое
и не понимает, чему верить. Экран должен быть один.

Уведомления в портал не переезжают намеренно: их смысл в том, что они
приходят сами, когда портал закрыт. Это ровно то, чего страница не умеет.

Процесс бота заодно крутит фоновый сбор. Сбору Telegram не нужен: он идёт
через прокси личек и продолжает работать, даже когда бот нем.

Запуск:
    export TELEGRAM_BOT_TOKEN=...  ADMIN_TG_IDS=123456789
    export ADMETRICS_KEY=...  WEBAPP_URL=https://admetrics.example.com
    python3 bot.py
"""

from __future__ import annotations

import html
import os
import re
import sys
import threading
import time
import traceback
from typing import Any

import requests

import collector
from core import alerts, store

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
ADMIN_TG_IDS = {x.strip() for x in os.environ.get("ADMIN_TG_IDS", "").split(",")
                if x.strip()}
# Сколько Telegram держит запрос `getUpdates` открытым, пока нет сообщений.
# Через прокси это НЕ бесплатная величина: всё это время по туннелю не идёт
# ни байта, а резидентные прокси рвут простаивающие туннели — и обрыв
# приходит как SSL EOF, будто сломался Telegram. Поэтому при заданном прокси
# окно короче: лишний запрос раз в двадцать секунд дешевле, чем бот, который
# половину времени переподключается.
POLL_TIMEOUT = int(os.environ.get(
    "TELEGRAM_POLL_SECONDS",
    "20" if os.environ.get("TELEGRAM_PROXY", "").strip() else "50"))
# Потолок паузы между попытками достучаться до Telegram.
MAX_RETRY = 300
# Как часто крутится фоновой цикл: сбор + рассылка алертов.
CYCLE_MINUTES = float(os.environ.get("BOT_CYCLE_MINUTES", "10"))
# Telegram режет сообщение на 4096 символах — режем сами, с запасом.
MSG_LIMIT = 3900
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
# Адрес портала. Без него кнопка «Открыть Admetrics» не появится, и бот
# превратится в тупик: впустил и не сказал куда.
PORTAL = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")

# Схема правится так же, как у прокси личек: при `socks5://` имя
# api.telegram.org разрешает НАША машина, а прокси мы завели именно потому,
# что с неё до Telegram не добраться. Запрос падал бы на DNS, не дойдя до
# прокси, — и бот молчал бы при живом и правильно настроенном прокси.
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()
_PROXIES = ({"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY}
            if TELEGRAM_PROXY else None)


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


# --------------------------------------------------------------------------- #
# Транспорт
# --------------------------------------------------------------------------- #
def mask_token(text: Any) -> str:
    """Убрать токен бота из текста.

    Библиотека сетевого клиента вкладывает в исключение весь URL, а токен
    Telegram живёт не в query-параметре, а прямо в пути (`/bot<токен>/getMe`).
    Из-за этого он уезжал в journalctl открытым при любой сетевой ошибке —
    то есть в лог, который смотрят и пересылают, попадал полный доступ к боту.
    Маскируем и по known-значению, и по форме пути: второе ловит случаи,
    когда в сообщении оказался чужой или прежний токен.
    """
    out = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot<скрыт>", str(text))
    if TOKEN:
        out = out.replace(TOKEN, "<скрыт>")
    return out


def api(method: str, **params: Any) -> dict[str, Any]:
    try:
        r = requests.post(f"{API}/{method}", json=params,
                          timeout=POLL_TIMEOUT + 10, proxies=_PROXIES)
        return r.json()
    except requests.RequestException as exc:
        # `network` отличает «не дозвонились» от «Telegram ответил отказом».
        # Первое лечится прокси и проходит само, второе — нет, и путать их
        # нельзя: на сетевой ошибке выходить из процесса неправильно.
        return {"ok": False, "network": True, "description": mask_token(exc)}


def send(chat_id: Any, text: str, preview: bool = False) -> None:
    """Отправить текст, разбив на части по границе строк.

    Разрезать по символам нельзя: посреди строки таблицы получится каша,
    а внутри <pre> ещё и сломается разметка.
    """
    for chunk in split_message(text):
        api("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML",
            disable_web_page_preview=not preview)


def split_message(text: str, limit: int = MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current.rstrip())
            # Одна строка длиннее лимита — только тогда режем жёстко.
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        parts.append(current.rstrip())
    return parts


# --------------------------------------------------------------------------- #
# Роли
# --------------------------------------------------------------------------- #
def whoami(tg_id: Any) -> dict[str, Any]:
    """Кто это. `role` — 'admin', 'marketer' или 'guest'."""
    tg = str(tg_id)
    with store.connect() as conn:
        found = store.rows(conn.execute(
            store._q("SELECT * FROM marketers WHERE tg_id = ? AND active = 1"),
            (tg,)))
    if tg in ADMIN_TG_IDS:
        person = found[0] if found else {"id": None, "name": "Админ"}
        return {**person, "role": "admin", "tg_id": tg}
    if found:
        return {**found[0], "role": found[0].get("role") or "marketer"}
    return {"id": None, "name": "", "role": "guest", "tg_id": tg}


# --------------------------------------------------------------------------- #
# Вход
# --------------------------------------------------------------------------- #
def portal_button() -> dict[str, Any] | None:
    """Кнопка «Открыть Admetrics». Без адреса портала кнопки нет —
    рисовать ссылку в никуда хуже, чем не рисовать её вовсе."""
    if not PORTAL.startswith("http"):
        return None
    return {"inline_keyboard": [[{"text": "Открыть Admetrics", "url": PORTAL}]]}


def send_portal(chat: Any, text: str) -> None:
    api("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
        disable_web_page_preview=True, reply_markup=portal_button())


NO_PORTAL = ("Портал не настроен: в <code>.env</code> нет "
             "<code>WEBAPP_URL</code>. Скажите администратору.")


def cmd_start(user: dict, args: str, chat: Any) -> None:
    """`/start` с кодом — привязка, без кода — просто ссылка на портал.

    Привязывается ОТПРАВИТЕЛЬ, а не чат: в группе chat_id общий, и запись
    его в `tg_id` отдала бы данные маркетолога всем участникам.
    """
    code = args.strip()
    who = str(user["tg_id"])

    # Уже свой — пускаем и не смотрим на код вовсе. Это первая и главная
    # проверка, а не последняя: Telegram ПОВТОРНО доставляет сообщение, пока
    # бот не подтвердил приём, и на рваной связи это происходит постоянно.
    # Первый проход гасит код и пытается ответить, ответ не уходит, приходит
    # тот же `/start КОД` — и человек, которого только что впустили, получал
    # «код уже использован» вместо приветствия.
    if user["role"] != "guest":
        return send_portal(chat, greeting(user))

    if not code:
        return send(chat, "Этот бот работает по приглашению. "
                          "Попросите у администратора ссылку для входа.")

    with store.connect() as conn:
        found = store.rows(conn.execute(store._q(
            "SELECT * FROM tg_link_codes WHERE code = ?"), (code,)))
        if not found:
            return send(chat, "Такой ссылки не существует. "
                              "Попросите новую у администратора.")
        row = found[0]
        if row["used_at"] and str(row["used_by_tg"] or "") != who:
            # Код погашен кем-то другим. Вот это действительно отказ.
            return send(chat, "Ссылка уже использована другим человеком. "
                              "Попросите свою у администратора.")
        # Свой код — гасим (повторно это безвредно) и привязываем.
        conn.execute(store._q(
            "UPDATE tg_link_codes SET used_at = ?, used_by_tg = ? WHERE code = ?"),
            (row["used_at"] or store.now(), who, code))
        conn.execute(store._q("UPDATE marketers SET tg_id = ? WHERE id = ?"),
                     (who, row["marketer_id"]))
    send_portal(chat, greeting(whoami(who), first=True))


def greeting(user: dict, first: bool = False) -> str:
    name = esc(user.get("name") or "")
    head = (f"Готово{', ' + name if name else ''} — доступ открыт."
            if first else f"Admetrics{', ' + name if name else ''}.")
    return (f"<b>{head}</b>\n\n"
            "Вся работа идёт на портале: кабинеты, открут по дням, кампании, "
            "бюджеты.\n\n"
            "Здесь я только предупрежу, если что-то сломается — "
            "отвалился доступ, кабинет забанили, сменилась карта."
            + ("" if PORTAL.startswith("http") else "\n\n" + NO_PORTAL))


def handle(update: dict[str, Any]) -> None:
    """Любое сообщение — просьба открыть портал. Команда одна: `/start`."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message["chat"]["id"]
    sender = (message.get("from") or {}).get("id")
    if sender is None:
        return
    user = whoami(sender)
    text = str(message.get("text") or "").strip()
    head, _, args = text.partition(" ")

    if head.split("@")[0].lower() == "/start":
        try:
            return cmd_start(user, args, chat)
        except Exception as exc:  # noqa: BLE001 — бот не должен падать от входа
            traceback.print_exc()
            return send(chat, f"Ошибка: {esc(exc)}")

    if user["role"] == "guest":
        return send(chat, "Этот бот работает по приглашению. "
                          "Попросите у администратора ссылку для входа.")
    send_portal(chat, greeting(user))


def handle_callback(update: dict[str, Any]) -> None:
    """Кнопок с колбэками у бота больше нет. Но старые сообщения у людей
    остались, и нажатие на них должно получать ответ, а не тишину:
    неотвеченный колбэк Telegram показывает как вечные часики."""
    cq = update.get("callback_query")
    if not cq:
        return
    api("answerCallbackQuery", callback_query_id=cq["id"],
        text="Эти кнопки больше не работают — всё переехало на портал.",
        show_alert=True)
    chat = (cq.get("message") or {}).get("chat", {}).get("id")
    if chat:
        send_portal(chat, greeting(whoami(cq["from"]["id"])))


# --------------------------------------------------------------------------- #
# Уведомления и фоновый сбор
# --------------------------------------------------------------------------- #
def deliver_alerts() -> int:
    """Разослать неотправленные алерты владельцам и админам.

    Это вторая и последняя работа бота. Уведомление приходит само, когда
    портал закрыт, — ради этого он и остался.
    """
    sent = 0
    mark = {"bad": "🔴", "warn": "🟡", "info": "🔵"}
    for alert in alerts.undelivered():
        text = (f"{mark.get(alert['severity'], '•')} <b>{esc(alert['title'])}</b>"
                + (f"\n{esc(alert['body'])}" if alert["body"] else ""))
        targets = set(ADMIN_TG_IDS)
        if alert.get("recipient_tg"):
            targets.add(str(alert["recipient_tg"]))
        for chat in targets:
            send_portal(chat, text)
        alerts.mark_delivered(alert["id"])
        sent += 1
    return sent


def background_loop() -> None:
    """Сбор и рассылка. Первый прогон — сразу, чтобы не ждать цикл."""
    while True:
        try:
            collector.run(days=int(os.environ.get("ADMETRICS_WINDOW_DAYS", "7")))
        except Exception:  # noqa: BLE001 — поток не должен умирать
            traceback.print_exc()
        try:
            deliver_alerts()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(max(CYCLE_MINUTES, 1) * 60)


def wait_for_telegram() -> dict[str, Any] | None:
    """Дождаться Telegram. None — токен недействителен, ждать бессмысленно.

    Раньше процесс выходил с ошибкой при любой неудаче getMe, systemd поднимал
    его снова, и так по кругу каждые несколько секунд. На сервере, где
    api.telegram.org режет провайдер, это давало бесконечную ленту падений
    вместо одного внятного сообщения — и, что хуже, вместе с ботом каждый раз
    умирал фоновый сбор, которому Telegram не нужен вовсе.

    Сетевую недоступность пережидаем с растущей паузой: она может кончиться
    сама (поднялся прокси, отпустила блокировка). Отказ самого Telegram —
    другое дело: 401 означает, что токен перевыпустили, и ждать нечего.
    """
    delay, said = 10, False
    while True:
        me = api("getMe")
        if me.get("ok"):
            if said:
                print("Telegram снова доступен.", flush=True)
            return me
        if me.get("error_code") == 401:
            print("Telegram не принимает токен (401) — его перевыпустили. "
                  "Впишите новый в .env.", file=sys.stderr)
            return None
        if not said:
            said = True
            print(f"Telegram недоступен: {mask_token(me.get('description'))}",
                  file=sys.stderr)
            if me.get("network") and not TELEGRAM_PROXY:
                print("TELEGRAM_PROXY не задан. Если api.telegram.org режет "
                      "провайдер, пропишите в .env строку вида\n"
                      "  TELEGRAM_PROXY=socks5://пользователь:пароль@хост:порт\n"
                      "Сбор при этом идёт своим чередом — ему Telegram не нужен.",
                      file=sys.stderr)
            print(f"Жду и пробую снова, пауза растёт до {MAX_RETRY // 60} мин.",
                  file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, MAX_RETRY)


def main() -> int:
    if not TOKEN:
        print("Не задан TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2
    store.init_db()

    # Сбор запускается ДО связи с Telegram и не зависит от неё: собирать
    # статистику через прокси личек можно и тогда, когда бот нем.
    threading.Thread(target=background_loop, daemon=True).start()

    me = wait_for_telegram()
    if me is None:
        return 2
    api("setMyCommands", commands=[])
    global BOT_USERNAME
    BOT_USERNAME = me["result"].get("username") or BOT_USERNAME
    print(f"Бот @{BOT_USERNAME} запущен: вход и уведомления. "
          f"Админы: {sorted(ADMIN_TG_IDS) or 'не заданы!'}")
    if not PORTAL.startswith("http"):
        print("ВНИМАНИЕ: WEBAPP_URL не задан — кнопки на портал не будет, "
              "и бот станет тупиком.", file=sys.stderr)
    if not ADMIN_TG_IDS:
        print("ВНИМАНИЕ: ADMIN_TG_IDS пуст — уведомления некому слать.",
              file=sys.stderr)

    offset = 0
    warned_conflict = False
    # Одиночный обрыв — это норма жизни через прокси, а не пропавшая связь.
    # Раньше любой из них отправлял бота ждать с паузой до пяти минут, и на
    # рваном туннеле он почти всё время ждал вместо того, чтобы работать.
    # В ожидание уходим только после нескольких неудач подряд.
    DROPS_BEFORE_WAIT = 4
    drops = 0
    while True:
        resp = api("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
        if not resp.get("ok"):
            code = resp.get("error_code")
            if resp.get("network"):
                drops += 1
                if drops < DROPS_BEFORE_WAIT:
                    # Переустанавливаем соединение сразу: следующий же запрос
                    # обычно проходит, потому что рвётся туннель, а не сеть.
                    time.sleep(2)
                    continue
                print(f"Связь рвётся подряд {drops} раз — жду её возвращения.",
                      file=sys.stderr)
                if wait_for_telegram() is None:
                    return 2
                drops = 0
                continue
            if code == 401:
                print("Telegram больше не принимает токен (401). "
                      "Похоже, его перевыпустили. Выхожу.", file=sys.stderr)
                return 2
            if code == 409 and not warned_conflict:
                warned_conflict = True
                print("ВНИМАНИЕ: этот токен уже опрашивает другой процесс. "
                      "Найдите его: ps aux | grep -i python", file=sys.stderr)
            time.sleep(5)
            continue
        warned_conflict = False
        drops = 0
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    handle_callback(update)
                else:
                    handle(update)
            except Exception:  # noqa: BLE001
                traceback.print_exc()


if __name__ == "__main__":
    sys.exit(main())
