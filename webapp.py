#!/usr/bin/env python3
"""
Telegram Mini App: панель Admetrics.

Зачем поверх бота. Текстовый бот упирается в свой формат: 4096 символов на
сообщение, таблицы разъезжаются, графика нет, а креатив виден только ссылкой.
Здесь всё это показывается как есть — таблицы, динамика, картинки объявлений.

Слой данных общий с ботом (`core/store.py`), поэтому цифры не могут разойтись
между панелью и ботом: их считает один и тот же код.

Сервер намеренно на стандартной библиотеке. Веб-стек из проекта недавно
выпилили, и тащить его обратно ради пяти JSON-ручек незачем: нагрузка здесь —
несколько человек, а не публичный трафик.

Авторизация — `initData` от Telegram: клиент подписывает данные о пользователе
ключом, производным от токена бота. Подпись проверяется на КАЖДЫЙ запрос, иначе
`user_id` можно было бы просто подставить в заголовок.

Запуск:
    python3 webapp.py                 # http://127.0.0.1:8080
    python3 webapp.py --tunnel        # плюс HTTPS-туннель cloudflared
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import traceback
import time
import urllib.parse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core import alerts, avatars, cycles, platforms, smm, store, ui
from core.graph import (AuthError, parse_proxy, probe_proxy,
                        remote_dns_variant, dns_failed,
                        secret_is_complete, split_secret_and_proxy)

HERE = Path(__file__).parent
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_TG_IDS = {x.strip() for x in os.environ.get("ADMIN_TG_IDS", "").split(",")
                if x.strip()}
# Сколько живёт подпись. Сутки: панель открывают и оставляют висеть, но
# бесконечно доверять старому initData нельзя — телефон могли потерять.
MAX_AUTH_AGE = 86400
# Подпись Login Widget живёт минутами: это одноразовый вход, а не сессия.
LOGIN_MAX_AGE = 600
# Сессия портала. Неделя: заходят с рабочего компьютера, каждый день
# логиниться заново — повод перестать пользоваться.
SESSION_MAX_AGE = 7 * 86400
SESSION_COOKIE = "admetrics_session"
# Куда webapp кладёт публичный адрес туннеля, чтобы бот показал кнопку.
URL_FILE = HERE / ".webapp_url"


# --------------------------------------------------------------------------- #
# Авторизация
# --------------------------------------------------------------------------- #
def check_init_data(init_data: str) -> dict[str, Any] | None:
    """Разобрать и проверить подпись initData. None — подпись не сошлась.

    Схема Telegram: ключ = HMAC(«WebAppData», токен бота), подпись = HMAC этим
    ключом от строки «ключ=значение», отсортированной по имени поля. Сравнение
    обязано быть постоянного времени — иначе подпись подбирается побайтово.
    """
    if not init_data or not TOKEN:
        return None
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)
    received = data.pop("hash", "")
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if time.time() - int(data.get("auth_date", 0)) > MAX_AUTH_AGE:
            return None
    except (TypeError, ValueError):
        return None
    try:
        return json.loads(data.get("user") or "{}")
    except json.JSONDecodeError:
        return None


def check_login_widget(params: dict[str, str]) -> dict[str, Any] | None:
    """Проверить подпись Telegram Login Widget. None — не сошлась.

    Осторожно: схема НЕ та же, что у Mini App. У initData ключ считается как
    HMAC(«WebAppData», токен), а у виджета — просто SHA256 от токена. Перепутать
    их легко, и тогда вход молча не работает при формально верной подписи.

    Подписанные поля приходят плоско (id, first_name, username, auth_date…),
    а не одним JSON-полем `user`, — это второе отличие от initData.
    """
    if not TOKEN or not params.get("hash"):
        return None
    data = {k: v for k, v in params.items() if k != "hash"}
    if not data.get("id"):
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, params["hash"]):
        return None
    # Свежесть обязательна: без неё однажды подсмотренная ссылка входа
    # работала бы вечно. Виджет отдаёт подпись на минуты, не на сутки.
    try:
        if time.time() - int(data.get("auth_date", 0)) > LOGIN_MAX_AGE:
            return None
    except (TypeError, ValueError):
        return None
    return data


# --------------------------------------------------------------------------- #
# Сессии портала
# --------------------------------------------------------------------------- #
def _session_key() -> bytes:
    """Ключ подписи кук. Выводится из токена бота отдельной меткой.

    Своего секрета в конфиге сознательно не заводим: это ещё одно значение,
    которое надо породить, положить в .env и не потерять. Побочный эффект
    полезен — при перевыпуске токена все сессии становятся недействительными
    разом, а именно этого и хочется, когда токен скомпрометирован.
    """
    return hmac.new(b"AdmetricsSession", TOKEN.encode(), hashlib.sha256).digest()


def make_session(tg_id: Any) -> str:
    issued = int(time.time())
    body = f"{tg_id}.{issued}"
    sig = hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(cookie: str) -> str | None:
    """tg_id из куки, если подпись верна и срок не вышел. Иначе None."""
    parts = (cookie or "").split(".")
    if len(parts) != 3:
        return None
    tg_id, issued, sig = parts
    body = f"{tg_id}.{issued}"
    expected = hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        if time.time() - int(issued) > SESSION_MAX_AGE:
            return None
    except (TypeError, ValueError):
        return None
    return tg_id


def whoami(tg_id: Any) -> dict[str, Any]:
    """Кто это. Тот же разбор ролей, что в боте, по тем же таблицам."""
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
# Страница входа
# --------------------------------------------------------------------------- #
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@").strip()
PUBLIC_URL = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")


def login_page(note: str = "") -> bytes:
    """Экран входа. Единственная страница портала, доступная без сессии.

    Кнопку рисует сам Telegram своим скриптом — подделать её нельзя, потому
    что подпись считается на их стороне ключом от токена бота. От нас нужны
    только имя бота и домен, разрешённый в BotFather (`/setdomain`): без него
    виджет молча не появится, и это самая частая причина «кнопки нет».
    """
    ready = bool(BOT_USERNAME and PUBLIC_URL.startswith("https://"))
    widget = (f'<script async src="https://telegram.org/js/telegram-widget.js?22"'
              f' data-telegram-login="{ui.esc(BOT_USERNAME)}" data-size="large"'
              f' data-userpic="false" data-radius="10"'
              f' data-auth-url="{ui.esc(PUBLIC_URL)}/auth/telegram"'
              f' data-request-access="write"></script>') if ready else (
        '<div class="note">Вход через Telegram не настроен. Нужны две '
        'переменные в <code>.env</code>: <code>TELEGRAM_BOT_USERNAME</code> '
        '(имя бота без @) и <code>WEBAPP_URL</code> (адрес портала по https), '
        'а в @BotFather — команда <code>/setdomain</code> с этим же адресом.</div>')

    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admetrics — вход</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
         background: #f6f7f9; color: #16181d; padding: 24px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14161a; color: #e9eaec; }}
    .card {{ background: #1c1f25 !important; border-color: #2a2e36 !important; }}
    .note {{ background: #2a2118 !important; border-color: #4a3a22 !important; }}
  }}
  .card {{ background: #fff; border: 1px solid #e3e6ea; border-radius: 16px;
          padding: 32px; max-width: 380px; width: 100%; text-align: center; }}
  h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: -0.2px; }}
  .sub {{ margin: 0 0 24px; opacity: .65; font-size: 14px; }}
  .note {{ background: #fff6e6; border: 1px solid #ffd591; border-radius: 10px;
          padding: 12px 14px; font-size: 13px; text-align: left;
          margin-bottom: 18px; }}
  code {{ font-size: 12px; }}
  .foot {{ margin-top: 22px; font-size: 12px; opacity: .55; }}
</style></head>
<body>
  <div class="card">
    <h1>Admetrics</h1>
    <p class="sub">Учёт кабинетов и расходов</p>
    {f'<div class="note">{note}</div>' if note else ''}
    {widget}
    <p class="foot">Доступ выдаёт администратор ссылкой-приглашением в боте.
      Без неё вход не сработает даже с верной подписью.</p>
  </div>
</body></html>""".encode("utf-8")


# --------------------------------------------------------------------------- #
# Сборка ответов
# --------------------------------------------------------------------------- #
def jsonable(value: Any) -> Any:
    """Decimal → float, дата → строка. Иначе json.dumps падает на деньгах."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def visible_accounts(user: dict) -> list[dict[str, Any]]:
    """Кабинеты, которые человеку положено видеть."""
    if user["role"] == "admin":
        return store.list_accounts()
    return ui.accounts_of(user["id"]) if user.get("id") else []


def may_see_account(user: dict, act_id: str) -> bool:
    return (user["role"] == "admin"
            or ui.owns_account(user.get("id"), act_id))


def api_summary(user: dict, params: dict) -> dict[str, Any]:
    period = params.get("period", "today")
    since, until, label = ui.parse_period(period)
    cycle = cycles.current_cycle()
    mid = None if user["role"] == "admin" else user.get("id")

    tree = store.founder_tree(since=since, until=until)
    if mid:
        mine = next((m for m in tree["marketers"]
                     if m["marketer_id"] == mid), None)
        total = store.dec(mine["spend_base"]) if mine else Decimal(0)
        unconverted = store.dec(mine["unconverted"]) if mine else Decimal(0)
        marketers = [mine] if mine else []
    else:
        total = store.dec(tree["total_base"])
        unconverted = sum((store.dec(m["unconverted"])
                           for m in tree["marketers"]), Decimal(0))
        marketers = tree["marketers"]

    accounts = visible_accounts(user)
    access = store.access_by_account()
    no_access = sum(1 for a in accounts
                    if (access.get(a["act_id"]) or {}).get("status") == "needs_reconnect")
    idents = store.list_identities(mid)
    run = ui.last_run()

    # Ближайшие списания FB — то, что маркетолог хочет знать первым.
    charges = []
    for a in accounts:
        f = store.billing_forecast(a["act_id"])
        if f.get("known"):
            charges.append({"act_id": a["act_id"], "name": a["name"] or a["act_id"],
                            "when": f["when"], "days_left": f["days_left"],
                            "amount": f["expected_amount"], "rule": f["rule"],
                            "currency": f["currency"]})
    charges.sort(key=lambda c: c["days_left"] if c["days_left"] is not None else 999)

    # Две итоговые суммы за период: сколько ушло с карт (MyBroCards) и сколько
    # FB предъявил по кабинетам. Байеру важна именно эта пара рядом, а не
    # список отдельных транзакций — его убрали как шум.
    card_tot = store.card_payment_totals(since, until, mid or "")
    fb_tot = store.fb_billing_total(since, until, mid or "")
    pay = {
        "cards_charged": store.dec(card_tot.get("charged") or 0),
        "cards_settled": store.dec(card_tot.get("settled") or 0),
        "cards_currency": card_tot.get("currency") or "",
        "fb_billed": store.dec(fb_tot.get("amount") or 0),
        "fb_currency": fb_tot.get("currency") or "",
    }
    return {
        "pay": pay,
        "role": user["role"], "name": user.get("name") or "Админ",
        "tg_id": user.get("tg_id"), "me_id": user.get("id"),
        "period": period, "period_label": label,
        "cycle_label": cycles.label(cycle),
        "base_currency": ui.BASE_CURRENCY,
        "spend_base": total, "unconverted": unconverted,
        "missing_rates": sorted({c for c, _ in tree["missing_rates"]}),
        "accounts_total": len(accounts),
        "accounts_broken": sum(1 for a in accounts if a["account_status"] != 1),
        "identities_total": len(idents),
        "identities_dead": sum(1 for i in idents
                               if i["status"] == "needs_reconnect"),
        "accounts_no_access": no_access,
        "last_run": run,
        "marketers": [{"id": m["marketer_id"], "name": m["name"],
                       "spend_base": m["spend_base"],
                       "unconverted": m["unconverted"]} for m in marketers],
        "alerts": ui._visible_alerts(user, 8),
        "upcoming_charges": charges[:6],
    }


def api_accounts(user: dict, params: dict) -> dict[str, Any]:
    """Список кабинетов со сводкой за период.

    Итоги берутся одной группировкой на всех: запрос на каждый кабинет
    превращал бы открытие списка в сотни обращений к базе.
    """
    since, until, label = ui.parse_period(params.get("period", "today"))
    totals = store.account_totals_map(since, until)
    access = store.access_by_account()
    empty = {"spend": 0, "impressions": 0, "clicks": 0, "ctr": 0, "cpc": 0,
             "cpm": 0, "leads": 0, "cpl": 0, "purchases": 0, "revenue": 0,
             "roas": 0}
    # Имена маркетологов по id — чтобы подставить владельца по доступу, когда
    # формальный не назначен.
    mnames = {m["id"]: m["name"] for m in store.list_marketers()}
    out = []
    for a in visible_accounts(user):
        t = totals.get(a["act_id"], empty)
        acc = access.get(a["act_id"]) or {}
        # Эффективный владелец: формально назначенный (ad_account_owner), а
        # если его нет — маркетолог, чья личка ИМЕЕТ ДОСТУП к кабинету.
        # Кабинет «лежит у Бориса», даже если владельца ему явно не проставили:
        # он пришёл через личку Бориса. Помечаем, что это по доступу, а не по
        # владению — чтобы админ при желании закрепил владельца явно.
        eff_mid = a.get("marketer_id") or acc.get("marketer_id") or ""
        eff_name = a.get("owner_name") or (mnames.get(eff_mid, "") if eff_mid else "")
        by_access = not a.get("marketer_id") and bool(acc.get("marketer_id"))
        out.append({**a, "spend": t["spend"], "impressions": t["impressions"],
                    "clicks": t["clicks"], "ctr": t["ctr"], "cpc": t["cpc"],
                    "cpm": t["cpm"],
                    # Пиксельные метрики: считаются в `_with_derived` вместе
                    # с CTR, поэтому здесь их достаточно перенести в ответ.
                    "leads": t.get("leads", 0), "cpl": t.get("cpl", 0),
                    "purchases": t.get("purchases", 0),
                    "revenue": t.get("revenue", 0), "roas": t.get("roas", 0),
                    "token_status": acc.get("status", ""),
                    "eff_marketer_id": eff_mid, "eff_owner_name": eff_name,
                    "owner_by_access": by_access,
                    "session_name": acc.get("session_name") or acc.get("label") or ""})
    out.sort(key=lambda r: -float(r["spend"] or 0))
    return {"period_label": label, "accounts": out,
            "marketers": [{"id": m["id"], "name": m["name"]}
                          for m in store.list_marketers()],
            "total_spend": sum(float(r["spend"] or 0) for r in out)}


def api_account(user: dict, params: dict) -> dict[str, Any]:
    act_id = params.get("act_id", "")
    if not may_see_account(user, act_id):
        return {"error": "Это не ваш кабинет"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    account = store.get_ad_account(act_id) or {}
    owner_id = store.owner_at(act_id, date.today().isoformat())
    owner = next((m for m in store.list_marketers() if m["id"] == owner_id), None)
    return {
        "period_label": label,
        "account": {**account, "owner_name": owner["name"] if owner else ""},
        "totals": store.account_totals(act_id, since, until),
        "billing": store.billing_forecast(act_id),
        "access": store.account_access(act_id),
        "adsets": store.account_adsets(act_id, since, until),
        # Списания FB — отдельным блоком, а не «уточнением» спенда: FB
        # списывает по порогу, поэтому расхождение с insights законно.
        "transactions": store.account_transactions(act_id, since, until, 100),
        "tx_totals": store.account_transaction_totals(act_id, since, until),
        # Контроль: кто имеет доступ, что менялось, есть ли пиксель.
        # Журнал не ограничиваем периодом экрана — «кто срезал бюджет»
        # спрашивают именно тогда, когда период уже пролистали.
        "users": store.account_users(act_id),
        "activities": store.account_activities(act_id, limit=40),
        "pixels": store.account_pixels(act_id),
        # Автоправила — рядом с журналом: журнал показывает, что бюджет
        # срезан, правило объясняет почему. Порознь каждый отвечает
        # на половину вопроса.
        "rules": store.account_rules(act_id),
        # Что делали МЫ. Рядом с журналом Meta, но отдельно: там «что
        # изменилось», здесь «кто из наших это сделал и что было до».
        "actions": store.account_actions(act_id, limit=20),
        "daily": store.account_daily(act_id, since, until),
        "countries": store.account_countries(act_id, since, until),
        "ads": (store.top_ads(act_id, since, until, limit=20)
                or store.ads_without_spend(act_id, limit=20)),
        "breakdowns": {kind: store.breakdown_totals(act_id, kind, since, until)
                       for kind, _ in ui.BREAKDOWN_TITLES},
    }


def api_identities(user: dict, params: dict) -> dict[str, Any]:
    since, until, label = ui.parse_period(params.get("period", "today"))
    mid = None if user["role"] == "admin" else user.get("id")
    totals = store.identity_totals_map(since, until)
    access = store.identity_accounts_map()
    empty = {"spend": 0, "impressions": 0, "clicks": 0, "ctr": 0,
             "currency": ""}
    out = []
    for i in store.list_identities(mid):
        t = totals.get(i["id"], empty)
        out.append({**i, "spend": t["spend"], "clicks": t["clicks"],
                    "impressions": t["impressions"], "ctr": t["ctr"],
                    "currency": t.get("currency") or "",
                    "accounts": len(access.get(i["id"], []))})
    out.sort(key=lambda r: -float(r["spend"] or 0))
    return {"period_label": label, "identities": out,
            # `me` пустой у админа: сводный экран по всей команде, и заводить
            # отсюда некому — непонятно, кому именно личика достанется.
            "me": mid or "", "role": user["role"],
            "total_spend": sum(float(r["spend"] or 0) for r in out)}


def api_identity(user: dict, params: dict) -> dict[str, Any]:
    ident_id = params.get("id", "")
    found = [i for i in store.list_identities() if i["id"] == ident_id]
    if not found:
        return {"error": "Личка не найдена"}
    i = found[0]
    if user["role"] != "admin" and i["marketer_id"] != user.get("id"):
        return {"error": "Чужая личка"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    return {"period_label": label, "identity": i,
            "totals": store.identity_totals(ident_id, since, until),
            "daily": store.identity_daily(ident_id, since, until),
            "accounts": store.identity_accounts(ident_id),
            # Права токена и чего из нужного не хватает. Пустой список прав
            # означает «не проверялось», а не «прав нет» — это разные вещи,
            # и различает их клиент по длине `permissions`.
            "permissions": store.identity_permissions(ident_id),
            "missing_permissions": [{"permission": k, "why": v}
                                    for k, v in store.missing_permissions(ident_id)]}


# --------------------------------------------------------------------------- #
# Управление кампаниями и адсетами
# --------------------------------------------------------------------------- #
# Здесь мы впервые не читаем, а ПИШЕМ у Meta. Правила, общие для всех ручек
# ниже и осознанно жёсткие:
#
#   • право проверяется по кабинету, а не по тому, что нарисовано в клиенте:
#     параметры приходят от браузера, и «не показали кнопку» ничего не
#     закрывает;
#   • пишется журнал — и на успех, и на отказ, с прежним значением. «Нажал,
#     ничего не произошло» иначе не отличить от «не нажимал», а Meta хранит
#     только текущее значение, и прежнее потом взять неоткуда;
#   • наша копия обновляется сразу из ответа Meta, а не из нашего намерения:
#     иначе человек, только что остановивший кампанию, увидит её работающей
#     до следующего прогона сбора — и нажмёт ещё раз.
#
# Подтверждение спрашивает интерфейс. Сервер на слово не верит, но и второй
# раз не переспрашивает: один явный жест человека — одно изменение.

# Потолок на смену бюджета. Не запрет, а предохранитель от опечатки:
# лишний ноль в поле — это десятикратный открут за ночь, и заметят его
# утром. Выше потолка ручка отказывает и предлагает менять в Ads Manager,
# где такое решение принимают осознанно.
BUDGET_MAX_FACTOR = 5
BUDGET_MAX_ABSOLUTE = 10000


def _manage_target(user: dict, payload: dict) -> tuple[dict, dict, str]:
    """Общая подготовка: право на кабинет, личика с секретом, адаптер.

    Возвращает (adapter, identity, error). Ошибка — человеческим текстом:
    «нет доступа» и «личика мертва» требуют разных действий, и сливать их
    в одно сообщение значит отправить человека чинить не то.
    """
    act_id = str(payload.get("act_id") or "")
    if not act_id or not may_see_account(user, act_id):
        return {}, {}, "Это не ваш кабинет"
    ident = store.identity_for_account(act_id)
    if not ident:
        return {}, {}, "Для этого кабинета нет подключённой личики"
    if not ident.get("secret"):
        return {}, ident, "У личики нет сохранённого токена"
    try:
        adapter = platforms.adapter_for(ident.get("platform") or "fb",
                                        ident["secret"], ident.get("proxy") or "")
    except Exception as exc:  # noqa: BLE001
        return {}, ident, f"Не удалось подключиться: {exc}"

    # Статус в базе — ПОСЛЕДНЕЕ ИЗВЕСТНОЕ состояние, а не факт. Он мог
    # протухнуть: личика падала из-за сети, сеть починили, а флаг остался
    # с прошлого сбора. Отказывать по нему в действии, которое само умеет
    # проверить факт одним запросом, — значит отправлять человека
    # «переподключать токен», с которым всё в порядке.
    if ident.get("status") != "ok":
        try:
            adapter.whoami()
        except Exception as exc:  # noqa: BLE001
            human = getattr(exc, "human", None) or str(exc)
            return {}, ident, f"Личика не отвечает: {human[:250]}"
        store.set_identity_status(ident["id"], "ok", "")
        ident["status"] = "ok"
    return adapter, ident, ""


def _log(user: dict, payload: dict, **kw: Any) -> None:
    store.log_action({
        "actor_id": user.get("id") or "", "actor_name": user.get("name") or "",
        "actor_role": user.get("role") or "", "act_id": payload.get("act_id") or "",
        **kw})


def _check_budget(new_value: Any, old_value: Any) -> str:
    """Похоже ли новое значение на опечатку. Пустая строка — всё в порядке."""
    try:
        value = Decimal(str(new_value).replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError):
        return "Бюджет должен быть числом"
    if value <= 0:
        return "Бюджет должен быть больше нуля"
    if value > BUDGET_MAX_ABSOLUTE:
        return (f"Больше {BUDGET_MAX_ABSOLUTE} за раз отсюда не ставим — "
                "такие суммы задают в Ads Manager осознанно")
    old = Decimal(str(old_value or 0))
    if old > 0 and value > old * BUDGET_MAX_FACTOR:
        return (f"Это в {value / old:.0f} раз больше нынешних {old} — "
                f"похоже на опечатку. Больше чем в {BUDGET_MAX_FACTOR} раз "
                "за один шаг не поднимаем")
    return ""


def api_campaign_action(user: dict, payload: dict) -> dict[str, Any]:
    """Остановить, запустить или сменить бюджет кампании."""
    adapter, ident, err = _manage_target(user, payload)
    campaign_id = str(payload.get("campaign_id") or "")
    op = str(payload.get("op") or "")
    before = store.campaign_row(campaign_id) or {}
    name = before.get("name") or campaign_id
    if err:
        _log(user, payload, target_kind="campaign", target_id=campaign_id,
             target_name=name, op=op, status="error", error=err)
        return {"error": err}
    if not campaign_id:
        return {"error": "Кампания не указана"}

    try:
        if op in ("pause", "resume"):
            was = before.get("status") or ""
            state = adapter.set_campaign_status(campaign_id, op == "resume")
            store.set_campaign_local(campaign_id, state)
            _log(user, payload, target_kind="campaign", target_id=campaign_id,
                 target_name=name, op=op, before_val=was,
                 after_val=state.get("status", ""), status="ok")
            return {"ok": True, "campaign": _jsonable(state),
                    "message": ("Кампания остановлена" if op == "pause"
                                else "Кампания запущена")}

        if op == "budget":
            # Какой бюджет у кампании живёт — дневной или общий — решаем
            # по тому, какой у неё сейчас: подменять один другим значит
            # менять смысл кампании, а не её величину.
            lifetime = not Decimal(str(before.get("daily_budget") or 0)) > 0 \
                and Decimal(str(before.get("lifetime_budget") or 0)) > 0
            old = before.get("lifetime_budget" if lifetime else "daily_budget")
            bad = _check_budget(payload.get("value"), old)
            if bad:
                _log(user, payload, target_kind="campaign", target_id=campaign_id,
                     target_name=name, op=op, before_val=str(old),
                     after_val=str(payload.get("value")), status="refused", error=bad)
                return {"error": bad}
            value = Decimal(str(payload.get("value")).replace(",", "."))
            state = (adapter.set_campaign_budget(campaign_id, lifetime=value)
                     if lifetime else
                     adapter.set_campaign_budget(campaign_id, daily=value))
            store.set_campaign_local(campaign_id, state)
            _log(user, payload, target_kind="campaign", target_id=campaign_id,
                 target_name=name, op=op, before_val=str(old),
                 after_val=str(state.get("lifetime_budget" if lifetime
                                         else "daily_budget")), status="ok")
            return {"ok": True, "campaign": _jsonable(state),
                    "message": f"Бюджет: {old} → {value}"}

        return {"error": "Неизвестная операция"}
    except Exception as exc:  # noqa: BLE001 — сюда приходит и отказ Meta, и обрыв
        human = getattr(exc, "human", None) or str(exc)
        _log(user, payload, target_kind="campaign", target_id=campaign_id,
             target_name=name, op=op, status="error", error=human)
        return {"error": f"Meta отказала: {human}"}


def api_adset_action(user: dict, payload: dict) -> dict[str, Any]:
    """То же для адсета. Кампания — это цель и бюджет, а решение чаще
    принимается на уровне аудитории: встаёт не кампания, а одна связка."""
    adapter, ident, err = _manage_target(user, payload)
    adset_id = str(payload.get("adset_id") or "")
    op = str(payload.get("op") or "")
    before = store.adset_row(adset_id) or {}
    name = before.get("name") or adset_id
    if err:
        _log(user, payload, target_kind="adset", target_id=adset_id,
             target_name=name, op=op, status="error", error=err)
        return {"error": err}
    if not adset_id:
        return {"error": "Адсет не указан"}
    try:
        if op in ("pause", "resume"):
            was = before.get("status") or ""
            state = adapter.set_adset_status(adset_id, op == "resume")
            store.set_adset_local(adset_id, state)
            _log(user, payload, target_kind="adset", target_id=adset_id,
                 target_name=name, op=op, before_val=was,
                 after_val=state.get("status", ""), status="ok")
            return {"ok": True, "adset": _jsonable(state),
                    "message": ("Адсет остановлен" if op == "pause"
                                else "Адсет запущен")}
        if op == "budget":
            old = before.get("daily_budget")
            bad = _check_budget(payload.get("value"), old)
            if bad:
                _log(user, payload, target_kind="adset", target_id=adset_id,
                     target_name=name, op=op, before_val=str(old),
                     after_val=str(payload.get("value")), status="refused", error=bad)
                return {"error": bad}
            value = Decimal(str(payload.get("value")).replace(",", "."))
            state = adapter.set_adset_budget(adset_id, daily=value)
            store.set_adset_local(adset_id, state)
            _log(user, payload, target_kind="adset", target_id=adset_id,
                 target_name=name, op=op, before_val=str(old),
                 after_val=str(state.get("daily_budget")), status="ok")
            return {"ok": True, "adset": _jsonable(state),
                    "message": f"Бюджет: {old} → {value}"}
        return {"error": "Неизвестная операция"}
    except Exception as exc:  # noqa: BLE001
        human = getattr(exc, "human", None) or str(exc)
        _log(user, payload, target_kind="adset", target_id=adset_id,
             target_name=name, op=op, status="error", error=human)
        return {"error": f"Meta отказала: {human}"}


def _jsonable(state: dict[str, Any]) -> dict[str, Any]:
    """Decimal → число. json.dumps о Decimal не знает."""
    return {k: (float(v) if isinstance(v, Decimal) else v)
            for k, v in state.items()}


def api_campaign(user: dict, params: dict) -> dict[str, Any]:
    """Одна кампания целиком: её дни, её адсеты, её управление.

    Отдельная ручка, а не кусок `/api/campaigns`: карточка кампании
    открывается по одной, и тянуть дни и адсеты сразу для всех — это
    десятки лишних запросов ради того, что человек посмотрит один раз.
    """
    act_id = params.get("act_id", "")
    campaign_id = params.get("campaign_id", "")
    if not may_see_account(user, act_id):
        return {"error": "Это не ваш кабинет"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    row = store.campaign_row(campaign_id) or {}
    return {
        "period_label": label,
        "campaign": row,
        "daily": store.campaign_daily(act_id, campaign_id, since, until),
        "adsets": store.campaign_adsets(act_id, campaign_id, since, until),
        "ads": store.top_ads(act_id, since, until, limit=12,
                             campaign_id=campaign_id),
        "actions": store.account_actions(act_id, limit=15),
        # Кампании трекера, на которые льёт эта кампания FB. Обычно одна.
        "keitaro": store.keitaro_for_campaign(campaign_id),
        "keitaro_gaps": store.keitaro_gaps(campaign_id),
    }


# --------------------------------------------------------------------------- #
# Внешнее API: отдать наружу то, что собрали
# --------------------------------------------------------------------------- #
# Отдельный слой от портала, а не «те же ручки с другим ключом». Причина
# простая: у портала ответы заточены под экран — там period_label человеческим
# языком, суммы, уже сложенные для карточки, и состав полей меняется, как
# только меняется вёрстка. Внешний потребитель на таком контракте живёт ровно
# до первой правки интерфейса.
#
# Поэтому здесь: плоские строки, явные даты вместо «за всё время», курсор
# для страниц и версия в адресе. Только чтение — ключ, который умеет
# останавливать кампании, это чужие деньги в чужом файле конфигурации.

API_PREFIX = "/api/v1/"
API_MAX_LIMIT = 5000
API_DEFAULT_LIMIT = 1000


def _api_dates(params: dict) -> tuple[str, str, str]:
    """Границы выборки. Обязательные и явные — без умолчаний вроде «сегодня».

    Умолчание здесь опаснее отказа: потребитель, забывший даты, получил бы
    вчерашний день, принял его за весь период и не заметил бы.
    """
    since = str(params.get("since") or "").strip()
    until = str(params.get("until") or "").strip()
    ok = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if not ok.match(since) or not ok.match(until):
        return "", "", "Нужны since и until в формате YYYY-MM-DD"
    if since > until:
        return "", "", "since позже until"
    return since, until, ""


def _api_page(params: dict) -> tuple[int, int]:
    try:
        limit = int(params.get("limit") or API_DEFAULT_LIMIT)
    except ValueError:
        limit = API_DEFAULT_LIMIT
    try:
        offset = int(params.get("offset") or 0)
    except ValueError:
        offset = 0
    return max(1, min(limit, API_MAX_LIMIT)), max(0, offset)


def _api_scope(key: dict, act_ids: list[str]) -> list[str]:
    """Ключ, выпущенный на маркетолога, видит только его кабинеты.

    Ключ без маркетолога — общий, видит всё. Оба случая нужны: один для
    выгрузки всей команды в бухгалтерию, другой — чтобы отдать подрядчику
    ровно его кусок и не больше.
    """
    mid = key.get("marketer_id") or ""
    if not mid:
        return act_ids
    mine = {a["act_id"] for a in ui.accounts_of(mid)}
    return [a for a in act_ids if a in mine]


def api_v1_accounts(key: dict, params: dict) -> dict[str, Any]:
    """Реестр кабинетов: чтобы act_id из остальных ручек имел имя."""
    rows = store.list_accounts()
    mid = key.get("marketer_id") or ""
    if mid:
        mine = {a["act_id"] for a in ui.accounts_of(mid)}
        rows = [r for r in rows if r["act_id"] in mine]
    return {"accounts": [{
        "act_id": r["act_id"], "name": r.get("name") or "",
        "status": r.get("account_status"), "status_label": r.get("status_label") or "",
        "disable_label": r.get("disable_label") or "",
        "currency": r.get("currency") or "", "bm_id": r.get("bm_id") or "",
        "daily_spend_limit": r.get("daily_spend_limit"),
        "spend_cap": r.get("spend_cap"), "balance": r.get("balance"),
        "amount_spent": r.get("amount_spent"),
        "funding_last4": r.get("funding_last4") or "",
        "next_bill_date": r.get("next_bill_date"),
        "timezone": r.get("timezone_name") or "",
        "owner_id": store.owner_at(r["act_id"], date.today().isoformat()) or "",
    } for r in rows], "count": len(rows)}


def api_v1_spend(key: dict, params: dict) -> dict[str, Any]:
    """Открут по дням. Плоские строки: кабинет × дата, без вложенности.

    Уровень выбирается параметром `level`: по кабинету или по кампании.
    Складывать их между собой нельзя и не надо — это одни и те же деньги
    с разной детализацией.
    """
    since, until, err = _api_dates(params)
    if err:
        return {"error": err}
    limit, offset = _api_page(params)
    level = str(params.get("level") or "account").lower()
    if level not in ("account", "campaign"):
        return {"error": "level: account или campaign"}
    act_ids = _api_scope(key, [a for a in str(params.get("act_id") or "").split(",") if a]) \
        if params.get("act_id") else None
    rows = store.api_spend_rows(level, since, until, act_ids,
                                key.get("marketer_id") or "", limit, offset)
    return {"level": level, "since": since, "until": until,
            "rows": rows, "count": len(rows),
            "next_offset": offset + len(rows) if len(rows) == limit else None}


def api_v1_transactions(key: dict, params: dict) -> dict[str, Any]:
    """Фактические списания Facebook. Деньги, а не оценка по показам."""
    since, until, err = _api_dates(params)
    if err:
        return {"error": err}
    limit, offset = _api_page(params)
    rows = store.api_fb_transactions(since, until, key.get("marketer_id") or "",
                                     limit, offset)
    return {"since": since, "until": until, "rows": rows, "count": len(rows),
            "next_offset": offset + len(rows) if len(rows) == limit else None}


def api_v1_payments(key: dict, params: dict) -> dict[str, Any]:
    """Платежи с карт MyBroCards — вторая половина денежной картины."""
    since, until, err = _api_dates(params)
    if err:
        return {"error": err}
    limit, offset = _api_page(params)
    rows = store.api_card_payments(since, until, key.get("marketer_id") or "",
                                   limit, offset)
    return {"since": since, "until": until, "rows": rows, "count": len(rows),
            "next_offset": offset + len(rows) if len(rows) == limit else None}


def api_v1_links(key: dict, params: dict) -> dict[str, Any]:
    """Кампании Keitaro, их ссылки и расход по живым объявлениям.

    Ручка, ради которой затевалась связка с трекером: слева id кампании
    Keitaro, справа потраченные на неё деньги. Выручку подставляет тот, кто
    читает, — она живёт на стороне трекера.

    Только живые объявления и только найденные связи: остановленное
    объявление выручки уже не принесёт, а несопоставленное принесёт её мимо
    нас. И то и другое в выгрузке ломало бы ROI молча.
    """
    # Даты здесь НЕ обязательны, в отличие от остальных ручек. Кампания
    # трекера живёт дольше отчётного окна, и «сколько на неё ушло всего» —
    # более частый вопрос, чем «сколько за неделю». Без дат считаем весь
    # открут; с датами — сужаем.
    since = str(params.get("since") or "").strip()
    until = str(params.get("until") or "").strip()
    if since or until:
        since, until, err = _api_dates(params)
        if err:
            return {"error": err}
    limit, offset = _api_page(params)
    act_ids = _api_scope(key, [a for a in str(params.get("act_id") or "").split(",") if a]) \
        if params.get("act_id") else None
    rows = store.keitaro_live_links(since, until, key.get("marketer_id") or "",
                                    act_ids, limit, offset)
    return {"since": since or "все данные", "until": until or "все данные",
            "rows": rows, "count": len(rows),
            "next_offset": offset + len(rows) if len(rows) == limit else None}


def api_v1_creatives(key: dict, params: dict) -> dict[str, Any]:
    """Открут в разрезе креатива: id креатива, название, расход, показы, клики.

    Ручка для CREOSTER: там креатив — это карточка работы монтажёра, здесь —
    строка расхода. Связать их по имени файла нельзя (видео уникализируют,
    и от исходного имени ничего не остаётся), поэтому отдаём голые факты, а
    сопоставление живёт на принимающей стороне.

    Даты необязательны, как и у `links`: «сколько этот креатив съел всего» —
    более частый вопрос, чем «сколько за неделю». Без дат считаем весь открут.

    `only_active=1` оставляет только креативы, у которых есть хотя бы одно
    живое объявление.
    """
    since = str(params.get("since") or "").strip()
    until = str(params.get("until") or "").strip()
    if since or until:
        since, until, err = _api_dates(params)
        if err:
            return {"error": err}
    limit, offset = _api_page(params)
    act_ids = _api_scope(key, [a for a in str(params.get("act_id") or "").split(",") if a]) \
        if params.get("act_id") else None
    only_active = str(params.get("only_active") or "") in ("1", "true", "yes")
    rows = store.creative_rows(since, until, key.get("marketer_id") or "",
                               act_ids, only_active, limit, offset)
    # Картинки и ссылки на предпросмотр наружу не отдаём: они подписаны и
    # протухают, а потребителю нужны ключ, название и цифры.
    slim = [{k: r[k] for k in ("creative_id", "name", "spend", "impressions",
                               "clicks", "ads", "active", "first_day",
                               "last_day")} for r in rows]
    return {"since": since or "все данные", "until": until or "все данные",
            "rows": slim, "count": len(slim),
            "next_offset": offset + len(slim) if len(slim) == limit else None}


API_V1 = {
    "accounts": api_v1_accounts,
    "spend": api_v1_spend,
    "transactions": api_v1_transactions,
    "payments": api_v1_payments,
    "links": api_v1_links,
    "creatives": api_v1_creatives,
}


def api_links_export(user: dict, params: dict) -> dict[str, Any]:
    """То же, что `/api/v1/links`, но для кнопки в портале.

    Отдельно от внешнего API, потому что права здесь другие: там ключ,
    здесь роль. Маркетолог видит только свои кабинеты — как и везде.
    """
    # Период не спрашиваем: выгрузка всегда за всё время. Расход на кампанию
    # трекера копится дольше, чем экранный период, и отдавать сюда «за
    # сегодня» значило бы молча урезать то, что человек считает итогом.
    mid = "" if user["role"] == "admin" else (user.get("id") or "")
    rows = store.keitaro_live_links("", "", mid, None, limit=5000)
    return {"period_label": "за всё время", "rows": rows, "count": len(rows)}


def api_creatives(user: dict, params: dict) -> dict[str, Any]:
    """Креативы для вкладки в портале.

    Права те же, что везде: маркетолог видит только свои кабинеты. Период
    берём с экрана, но пустой период («всё время») здесь осмыслен и разрешён —
    креатив живёт дольше отчётной недели.
    """
    mid = "" if user["role"] == "admin" else (user.get("id") or "")
    period = str(params.get("period") or "")
    if period in ("", "all"):
        since = until = ""
        label = "за всё время"
    else:
        since, until, label = ui.parse_period(period)
    only_active = str(params.get("only_active") or "") in ("1", "true", "yes")
    rows = store.creative_rows(since, until, mid, None, only_active, limit=2000)
    total = {"spend": round(sum(r["spend"] for r in rows), 2),
             "impressions": sum(r["impressions"] for r in rows),
             "clicks": sum(r["clicks"] for r in rows),
             "creatives": len(rows),
             "no_id": sum(1 for r in rows if not r["creative_id"])}
    return {"period_label": label, "rows": rows, "total": total}


def api_keys_list(user: dict, params: dict) -> dict[str, Any]:
    """Реестр ключей. Секрета здесь нет — только префикс для опознания."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    return {"keys": store.list_api_keys(),
            "marketers": [{"id": m["id"], "name": m["name"]}
                          for m in store.list_marketers()],
            "base_url": (PUBLIC_URL or "") + API_PREFIX}


def api_keys_create(user: dict, payload: dict) -> dict[str, Any]:
    """Выпустить ключ. Сам ключ показывается ОДИН раз — здесь и сейчас.

    Хранить его, чтобы «посмотреть потом», значит завести второе место,
    откуда он может утечь. Потерявший ключ выпускает новый.
    """
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"error": "Назовите ключ — иначе через месяц не вспомнить, "
                         "чей он и можно ли его отозвать"}
    mid = str(payload.get("marketer_id") or "")
    if mid and not any(m["id"] == mid for m in store.list_marketers()):
        return {"error": "Маркетолог не найден"}
    raw, row = store.create_api_key(name, mid, "read", user.get("id") or "")
    return {"ok": True, "key": raw, "id": row["id"],
            "message": "Ключ показан один раз. Скопируйте его сейчас."}


def api_keys_revoke(user: dict, payload: dict) -> dict[str, Any]:
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    ok = store.revoke_api_key(str(payload.get("id") or ""))
    return {"ok": ok, "message": "Ключ отозван" if ok else "Ключ уже отозван"}

def api_adset(user: dict, params: dict) -> dict[str, Any]:
    """Объявления одного адсета. Грузится по раскрытию, а не заранее.

    В кабинете сотни объявлений; тянуть их все ради дерева, которое человек
    раскроет в двух местах, — это лишние секунды на каждом открытии экрана.
    """
    act_id = params.get("act_id", "")
    adset_id = params.get("adset_id", "")
    if not may_see_account(user, act_id):
        return {"error": "Это не ваш кабинет"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    found = store.adset_ads(act_id, adset_id, since, until)
    for a in found:
        a["issues"] = _issues(a.pop("issues_json", ""))
        # У креатива из поста `link_url` пуст, но ссылка известна: её
        # сохранил матчинг. Показать «куда льёт» надо в обоих случаях.
        if not a.get("link_url"):
            a["link_url"] = a.get("kt_link") or ""
    return {"period_label": label, "ads": found}


def api_campaigns(user: dict, params: dict) -> dict[str, Any]:
    act_id = params.get("act_id", "")
    if not may_see_account(user, act_id):
        return {"error": "Это не ваш кабинет"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    account = store.get_ad_account(act_id) or {}
    found = store.campaigns_of(act_id, since, until)
    for c in found:
        c["issues"] = _issues(c.pop("issues_json", ""))
    return {"period_label": label, "account": account, "campaigns": found,
            # Значок связи с трекером рисуется прямо в списке: иначе кампанию
            # без связи видно только если открыть её по одной.
            "keitaro_map": store.keitaro_map(act_id)}


def _issues(raw: Any) -> list[str]:
    """Тексты причин реджекта. Их формат Meta меняет, поэтому разбираем мягко."""
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    out = []
    for item in parsed if isinstance(parsed, list) else []:
        if isinstance(item, dict):
            text = (item.get("error_summary") or item.get("error_message")
                    or item.get("summary") or "")
            if text:
                out.append(str(text))
    return out


def api_businesses(user: dict, params: dict) -> dict[str, Any]:
    """Бизнес-менеджеры. Только админу: BM — общий актив, не личный.

    Маркетологу этот экран не нужен и вреден: он показывает чужие кабинеты
    внутри того же BM.
    """
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    found = store.list_businesses(since, until)
    bm_id = params.get("bm_id", "")
    return {"period_label": label, "businesses": found,
            "accounts": store.business_accounts(bm_id) if bm_id else [],
            # Доступ к BM сильнее доступа к кабинету: человек с ролью
            # в бизнесе дотягивается до всех кабинетов внутри, в том числе
            # до тех, которых нет в нашем реестре.
            "users": store.business_users(bm_id) if bm_id else [],
            "pages": store.business_pages(bm_id) if bm_id else [],
            "pixels": store.business_pixels(bm_id) if bm_id else [],
            "bm_id": bm_id}


def api_cards(user: dict, params: dict) -> dict[str, Any]:
    """Платежи с карт: итоги, разрез по людям и страница самих платежей.

    Маркетолог видит только свои — фильтр по `marketer_id` ставится здесь,
    а не в интерфейсе: параметры приходят от клиента, и «не нарисовали
    выпадашку» ничего не закрывает.
    """
    # По умолчанию — текущий цикл: платежей пятнадцать тысяч, и «сегодня»
    # на этом экране почти всегда пусто.
    since, until, label = ui.parse_period(
        params.get("period") or f"c{cycles.current_cycle()}")
    admin = user["role"] == "admin"
    mid = params.get("marketer_id", "") if admin else (user.get("id") or "")
    if not admin and not mid:
        return {"error": "Нет доступа"}
    team = params.get("team", "") if admin else ""
    card_id = params.get("card_id", "")
    state = params.get("state", "")
    # Номер страницы приходит от клиента — разбираем строго, иначе '²' уронит int().
    page = ui._num(str(params.get("page") or "0"), 0, 100000) or 0
    out = {
        "period_label": label,
        "totals": store.card_payment_totals(since, until, mid, team, card_id),
        **store.card_payments_page(since, until, mid, team, card_id, state,
                                   page, 50),
        "filters": store.card_payment_filters() if admin
                   else {"teams": [], "cards": []},
        "by_marketer": store.card_payments_by_marketer(since, until)
                       if admin else [],
        "marketers": [{"id": m["id"], "name": m["name"]}
                      for m in store.list_marketers()] if admin else [],
        "selected": {"marketer_id": mid, "team": team, "card_id": card_id,
                     "state": state},
    }
    return out


def api_marketers(user: dict, params: dict) -> dict[str, Any]:
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    tree = store.founder_tree(since=since, until=until)
    spend = {m["marketer_id"]: m for m in tree["marketers"]}
    accounts = store.list_accounts()
    eff = store.effective_account_owner()
    bids = store.marketer_brocard_ids()
    by_owner: dict[str, list[dict]] = {}
    for a in accounts:
        mid_a = eff.get(a["act_id"]) or a.get("marketer_id") or ""
        by_owner.setdefault(mid_a, []).append(a)
    out = []
    for m in store.list_marketers():
        mine = by_owner.get(m["id"], [])
        row = spend.get(m["id"], {})
        out.append({
            "id": m["id"], "name": m["name"], "linked": bool(m.get("tg_id")),
            "brocard_id": bids.get(m["id"], ""),
            "identities": m.get("identities") or 0,
            "dead_identities": m.get("dead_identities") or 0,
            "accounts": len(mine),
            "accounts_broken": sum(1 for a in mine if a["account_status"] != 1),
            "spend_base": row.get("spend_base", 0),
            "unconverted": row.get("unconverted", 0),
        })
    out.sort(key=lambda r: -float(r["spend_base"] or 0))
    orphan = by_owner.get("", [])
    return {"period_label": label, "marketers": out,
            "base_currency": ui.BASE_CURRENCY,
            "unassigned_accounts": len(orphan),
            "total_base": tree["total_base"]}


def api_alerts(user: dict, params: dict) -> dict[str, Any]:
    found = ui._visible_alerts(user, 60)
    names = {a["act_id"]: (a["name"] or a["act_id"])
             for a in store.list_accounts()}
    for r in found:
        r["account_name"] = names.get(r.get("act_id") or "", "")
    return {"alerts": found}


def api_health(user: dict, params: dict) -> dict[str, Any]:
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    with store.connect() as conn:
        runs = store.rows(conn.execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT 8"))
        idents = store.rows(conn.execute(
            "SELECT status, COUNT(*) AS n FROM identities GROUP BY status"))
        counts = {}
        for table in ("ad_accounts", "campaigns", "ads", "spend_daily",
                      "ad_daily", "spend_breakdown_daily"):
            counts[table] = store.rows(conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"))[0]["n"]
    return {"runs": runs, "identities": idents, "counts": counts,
            "overlaps": store.check_owner_overlaps(),
            "cycle": cycles.current_cycle(),
            "cycle_label": cycles.label(cycles.current_cycle())}


def api_recon(user: dict, params: dict) -> dict[str, Any]:
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    since, until, label = ui.parse_period(params.get("period", "today"))
    return {"period_label": label, **alerts.reconcile(since, until)}


def api_comments(user: dict, params: dict) -> dict[str, Any]:
    mid = None if user["role"] == "admin" else user.get("id")
    return {"pages": store.list_pages(mid), "role": user["role"]}


def api_comments_scan(user: dict, payload: dict) -> dict[str, Any]:
    """Подсчёт комментариев без удаления — безопасный предпросмотр."""
    page = _page_for(user, payload.get("page_id"))
    if not page:
        return {"error": "Страница не найдена или чужая"}
    import collector
    r = collector.clean_page_comments(page, delete=False)
    return {"ok": True, "comments": r["comments"], "posts": r["posts"]}


def api_comments_clean(user: dict, payload: dict) -> dict[str, Any]:
    """Удалить все комментарии страницы. Необратимо — идёт в фоне."""
    page = _page_for(user, payload.get("page_id"))
    if not page:
        return {"error": "Страница не найдена или чужая"}
    if not page.get("can_moderate"):
        return {"error": "У токена нет права модерации этой страницы"}

    def run() -> None:
        import collector
        try:
            collector.clean_page_comments(page, delete=True)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": "Удаление запущено — обновите через минуту."}


def api_comments_auto(user: dict, payload: dict) -> dict[str, Any]:
    """Включить/выключить постоянную автоочистку страницы."""
    page = _page_for(user, payload.get("page_id"))
    if not page:
        return {"error": "Страница не найдена или чужая"}
    if not page.get("can_moderate"):
        return {"error": "У токена нет права модерации этой страницы"}
    store.set_page_autoclean(page["page_id"], bool(payload.get("on")))
    return {"ok": True, "message": "Автоочистка "
            + ("включена" if payload.get("on") else "выключена")}


def _page_for(user: dict, page_id: Any) -> dict | None:
    """Страница с токеном, если она доступна этому пользователю."""
    page = store.get_page(str(page_id or ""))
    if not page:
        return None
    if user["role"] != "admin" and page.get("marketer_id") != user.get("id"):
        return None
    return page


def _proxy_in(raw: str) -> tuple[str, str]:
    """Разобрать прокси, введённый руками. Возвращает (url, ошибка).

    Проверяем на входе, а не на первом сборе: иначе строка, которую requests
    не разберёт, лежит в базе до ночи и выглядит как «Meta не отвечает».
    """
    url, err = parse_proxy(raw)
    return url, (f"Прокси не разобрал: {err}" if err else "")


def api_proxies(user: dict, params: dict) -> dict[str, Any]:
    """Пул прокси для выпадающего списка. Только подсказки, без учёток.

    Маркетолог видит свои. Чужой адрес в списке — это чужая учётка, пусть
    и за звёздочками: выбрав его, он отправил бы работать под своей личикой
    чужой прокси.
    """
    mine = None if user["role"] == "admin" else (user.get("id") or "")
    return {"proxies": store.list_proxies(mine)}


def api_proxy_add(user: dict, payload: dict) -> dict[str, Any]:
    url, err = _proxy_in(str(payload.get("url") or ""))
    if err:
        return {"error": err}
    if not url:
        return {"error": "Введите адрес прокси"}
    pid = store.add_proxy(url, str(payload.get("label") or "").strip(),
                          marketer_id="" if user["role"] == "admin"
                          else (user.get("id") or ""))
    return {"ok": True, "id": pid, "message": "Сохранено."}


def api_proxy_list(user: dict, params: dict) -> dict[str, Any]:
    """Пул прокси с числом занятых личек — для раздела «Прокси»."""
    mine = None if user["role"] == "admin" else (user.get("id") or "")
    return {"proxies": store.list_proxies_full(mine), "role": user["role"]}


def _dns_alternative(url: str, error: str) -> dict[str, Any]:
    """Если запрос упал на DNS — проверить тот же прокси с удалённым DNS.

    Сами не переключаем. Проверяем и показываем результат: «через socks5h://
    прошло за столько-то, выходим с такого-то IP» — и кнопку. Молча менять
    схему нельзя: строку даёт поставщик, и подменять её за спиной человека мы
    не будем. Но и оставлять его гадать, в чём дело, тоже не дело.
    """
    if not dns_failed(error):
        return {}
    alt = remote_dns_variant(url)
    if not alt:
        return {}
    res = probe_proxy(alt, timeout=12.0)
    out = {"alt_label": _proxy_label(alt), "alt_ms": res.get("ms")}
    if not res.get("ok"):
        # Сказать, что и так не вышло, обязательно. Молчание здесь читается
        # как «второй вариант не пробовали», и человек остаётся с тем же
        # вопросом, с которым пришёл.
        out["alt_failed"] = res.get("error", "")[:300]
        return out
    out.update({"alt": alt, "alt_ip": res.get("ip", "")})
    return out


def api_proxy_probe(user: dict, payload: dict) -> dict[str, Any]:
    """Проверить прокси сам по себе — до того, как к нему привязан токен.

    Берёт либо строку, которую человек только что вставил (тогда в пул она
    не попадает — сначала проверка, потом сохранение), либо адрес из пула
    по id.
    """
    mine = None if user["role"] == "admin" else (user.get("id") or "")
    raw = str(payload.get("url") or "").strip()
    if raw:
        url, err = _proxy_in(raw)
        if err:
            return {"error": err}
    else:
        url = store.get_proxy_url(str(payload.get("id") or ""), mine)
        if not url:
            return {"error": "Прокси не найден"}
    res = probe_proxy(url)
    res["via"] = _proxy_label(url)
    res["url"] = url
    if not res.get("ok"):
        res.update(_dns_alternative(url, res.get("error", "")))
    return res


def api_proxy_setdns(user: dict, payload: dict) -> dict[str, Any]:
    """Переключить прокси из пула на разрешение имени на его стороне.

    Только по явному нажатию и только после того, как проверка показала, что
    так он работает, а иначе нет.
    """
    mine = None if user["role"] == "admin" else (user.get("id") or "")
    pid = str(payload.get("id") or "")
    url = store.get_proxy_url(pid, mine)
    if not url:
        return {"error": "Прокси не найден"}
    alt = remote_dns_variant(url)
    if not alt:
        return {"error": "У этой схемы нет варианта с удалённым DNS"}
    hint = store.set_proxy_url(pid, alt)
    return {"ok": True, "message": f"Теперь {hint}"}


def api_proxy_delete(user: dict, payload: dict) -> dict[str, Any]:
    mine = None if user["role"] == "admin" else (user.get("id") or "")
    err = store.delete_proxy(str(payload.get("id") or ""), mine)
    return {"error": err} if err else {"ok": True, "message": "Прокси убран"}


def api_proxy_rename(user: dict, payload: dict) -> dict[str, Any]:
    mine = None if user["role"] == "admin" else (user.get("id") or "")
    ok = store.rename_proxy(str(payload.get("id") or ""),
                            str(payload.get("label") or ""), mine)
    return {"ok": True} if ok else {"error": "Прокси не найден"}


def api_identity_proxy(user: dict, payload: dict) -> dict[str, Any]:
    """Сменить прокси у личики, не трогая мультитокен.

    Отдельная операция, а не побочный эффект «заменить токен»: адрес меняют
    гораздо чаще токена, и требовать ради этого лезть в антик за токеном —
    лишняя работа и лишний риск ошибиться.
    """
    ident_id = str(payload.get("identity_id") or "")
    ident = store.get_identity(ident_id)
    if not ident:
        return {"error": "Личка не найдена"}
    if not (user["role"] == "admin"
            or ident.get("marketer_id") == user.get("id")):
        return {"error": "Это не ваша личика"}
    mine = None if user["role"] == "admin" else (user.get("id") or "")
    raw, err = _proxy_in(str(payload.get("proxy") or ""))
    if err:
        return {"error": err}
    pid = str(payload.get("proxy_id") or "")
    if payload.get("clear"):
        proxy = ""
    else:
        proxy = store.resolve_proxy(pid, raw, mine)
        if not proxy:
            return {"error": "Прокси не выбран"}
    hint = store.set_identity_proxy(ident_id, proxy)
    return {"ok": True, "hint": hint,
            "message": ("Прокси снят — личика пойдёт напрямую. "
                        "Проверьте доступ." if not proxy
                        else f"Прокси заменён на {hint}. Проверьте доступ.")}


def api_proxy_check(user: dict, payload: dict) -> dict[str, Any]:
    """Живая проверка доступа: доходит ли запрос до Meta и что она отвечает.

    Гадать, работает ли прокси, — худший способ: пустой сбор, мёртвая
    личика и отказ управления выглядят одинаково, а причины разные.
    Здесь один короткий запрос `me` даёт прямой ответ.
    """
    ident_id = str(payload.get("identity_id") or "")
    act_id = str(payload.get("act_id") or "")
    ident = (store.identity_for_account(act_id) if act_id
             else store.identity_with_secret(ident_id))
    if not ident:
        return {"error": "Личика не найдена"}
    if not (user["role"] == "admin"
            or ident.get("marketer_id") == user.get("id")):
        return {"error": "Это не ваша личика"}
    if not ident.get("secret"):
        return {"error": "У личики нет сохранённого токена"}
    proxy = ident.get("proxy") or ""
    started = time.time()
    try:
        adapter = platforms.adapter_for(ident.get("platform") or "fb",
                                        ident["secret"], proxy)
        me = adapter.whoami()
    except Exception as exc:  # noqa: BLE001
        human = getattr(exc, "human", None) or str(exc)
        # Проверка не только отчитывается, но и чинит запись: иначе человек
        # видит «Meta отвечает», а сбор и управление продолжают считать
        # личику мёртвой по флагу с прошлого прогона.
        if isinstance(exc, AuthError):
            store.set_identity_status(ident["id"], "needs_reconnect", human,
                                      getattr(exc, "code", None),
                                      getattr(exc, "subcode", None))
        out = {"ok": False, "via": _proxy_label(proxy),
               "error": human[:400], "was": ident.get("status", ""),
               "ms": int((time.time() - started) * 1000)}
        if proxy:
            # И по объяснённому тексту, и по сырому: признак DNS бывает
            # в любом из них, а сырой к тому же обрезан.
            alt = _dns_alternative(proxy, f"{human} {exc}")
            out.update(alt)
            if alt.get("alt"):
                # Исходный вариант не прошёл, а этот прошёл — переключаем
                # сразу, не спрашивая. Спрашивать имеет смысл, когда есть
                # выбор; здесь выбора нет: один адрес работает, другой нет.
                store.set_identity_proxy(ident["id"], alt["alt"])
                store.note_identity_issue(
                    ident["id"],
                    "прокси переведён на разрешение имени стороной прокси")
                out["switched"] = True
        return out
    fixed = ident.get("status") != "ok"
    if fixed:
        store.set_identity_status(ident["id"], "ok", "")
    return {"ok": True, "via": _proxy_label(proxy),
            "who": me.get("name") or me.get("id") or "",
            "fixed": fixed, "was": ident.get("status", ""),
            "ms": int((time.time() - started) * 1000)}


def _proxy_label(proxy: str) -> str:
    """Прокси для показа: схема и хост, без логина и пароля."""
    if not proxy:
        return "без прокси, напрямую"
    try:
        parsed = urllib.parse.urlparse(proxy)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or ''}"
    except ValueError:
        return "прокси задан"


def api_comments_auto_all(user: dict, payload: dict) -> dict[str, Any]:
    """Массово включить/выключить автоочистку на ВСЕХ страницах маркетолога."""
    mid = None if user["role"] == "admin" else user.get("id")
    n = store.set_all_pages_autoclean(mid, bool(payload.get("on")))
    return {"ok": True, "message": f"Автоочистка {'включена' if payload.get('on') else 'выключена'} "
            f"на {n} страницах"}


def api_comments_clean_all(user: dict, payload: dict) -> dict[str, Any]:
    """Удалить комментарии сразу на всех страницах маркетолога. В фоне."""
    mid = None if user["role"] == "admin" else user.get("id")
    pages = [p for p in store.list_pages(mid) if p.get("can_moderate")]
    if not pages:
        return {"error": "Нет страниц с правом модерации"}

    def run() -> None:
        import collector
        for row in pages:
            page = store.get_page(row["page_id"])
            if page:
                try:
                    collector.clean_page_comments(page, delete=True)
                except Exception:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": f"Удаление запущено на {len(pages)} страницах."}


def api_recovery(user: dict, params: dict) -> dict[str, Any]:
    """Данные восстановления аккаунта — только владельцу или админу.

    Отдельным запросом (а не в общей карточке), чтобы пароли и 2FA не висели
    на экране без нужды и не попадали в общий JSON карточки.
    """
    act_id = params.get("act_id", "")
    if not may_see_account(user, act_id):
        return {"error": "Это не ваш кабинет"}
    acc = store.account_access(act_id)
    session = (acc or {}).get("session_name") or (acc or {}).get("label") or ""
    if not session:
        return {"error": "У аккаунта нет имени сессии — нечем сопоставить с таблицей"}
    rec = store.get_recovery(session)
    if not rec:
        return {"error": f"В таблице нет строки для сессии «{session}»"}
    return {"recovery": rec, "session": session}


def api_recovery_search(user: dict, params: dict) -> dict[str, Any]:
    """Поиск по картотеке аккаунтов. Только админ.

    Картотека — это вся команда разом, и доступ к ней шире, чем «свои
    кабинеты». Маркетолог видит данные восстановления там же, где и раньше:
    в карточке своего кабинета, по его имени сессии.
    """
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    q = str(params.get("q") or "").strip()
    return {"rows": store.search_recovery(q), "q": q,
            **store.recovery_stats()}


def api_recovery_one(user: dict, params: dict) -> dict[str, Any]:
    """Одна строка картотеки по имени сессии — с секретами. Только админ."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    session = str(params.get("session") or "")
    rec = store.get_recovery(session)
    if not rec:
        return {"error": f"В картотеке нет строки «{session}»"}
    return {"recovery": rec, "session": session}


def api_fx_get(user: dict, params: dict) -> dict[str, Any]:
    """Текущие курсы валют (последний известный по каждой)."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    with store.connect() as conn:
        rows = store.rows(conn.execute("""
            SELECT currency, rate_to_base, MAX(date) AS date FROM fx_rates
             GROUP BY currency ORDER BY currency"""))
    return {"base": ui.BASE_CURRENCY, "rates": rows}


def api_fx_set(user: dict, payload: dict) -> dict[str, Any]:
    """Задать курс валюты и уплотнить его на весь период с данными.

    Ставим курс на сегодня и переносим его назад до самой ранней даты
    открута и вперёд с запасом — тогда пересчёт в рубли срабатывает на всех
    отчётах, а не только за сегодня.
    """
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    currency = str(payload.get("currency") or "USD").upper().strip()
    try:
        rate = float(str(payload.get("rate")).replace(",", "."))
    except (TypeError, ValueError):
        return {"error": "Курс должен быть числом, например 80.5"}
    if rate <= 0:
        return {"error": "Курс должен быть больше нуля"}

    from datetime import date, timedelta
    today = date.today()
    # Ранняя граница — минимальная дата открута, но не дальше ~2 лет.
    with store.connect() as conn:
        found = store.rows(conn.execute(
            "SELECT MIN(date) AS d FROM spend_daily"))
    floor = (today - timedelta(days=730)).isoformat()
    since = max((found[0]["d"] or floor), floor) if found else floor
    until = (today + timedelta(days=30)).isoformat()
    store.set_rate(today.isoformat(), currency, rate, source="панель")
    filled = store.fill_rate_gaps(currency, since, until)
    return {"ok": True, "message": f"Курс {currency} = {rate} {ui.BASE_CURRENCY} "
            f"задан и растянут на {filled} дней ({since}..{until})."}


def api_retoken(user: dict, payload: dict) -> dict[str, Any]:
    """Заменить мультитокен личики прямо из панели.

    Здесь это удобнее, чем в боте: поле ввода не упирается в 4096 символов,
    поэтому контейнер не приходится слать частями, и он вообще не попадает
    в переписку — то есть его не нужно потом оттуда удалять.
    """
    ident_id = str(payload.get("id") or "")
    found = [i for i in store.list_identities() if i["id"] == ident_id]
    if not found:
        return {"error": "Личка не найдена"}
    ident = found[0]
    if user["role"] != "admin" and ident["marketer_id"] != user.get("id"):
        return {"error": "Чужая личка"}
    if ident["status"] == "revoked":
        return {"error": "Секрет отозван — заводите новую личку"}

    secret, inline_proxy = split_secret_and_proxy(str(payload.get("secret") or ""))
    if not secret_is_complete(secret):
        return {"error": "Это не похоже на целый мультитокен: жду контейнер "
                         "FBTOOL или токен EAA. Проверьте, что скопировали "
                         "его полностью."}
    # Прокси: новый введённый (уйдёт в пул) либо выбранный из пула по id.
    typed, err = _proxy_in(str(payload.get("proxy") or inline_proxy))
    if err:
        return {"error": err}
    proxy = store.resolve_proxy(
        str(payload.get("proxy_id") or ""), typed,
        None if user["role"] == "admin" else (user.get("id") or ""))
    store.replace_identity_secret(ident_id, secret, proxy=proxy)
    return {"ok": True, "message": "Токен заменён. Проверю на ближайшем сборе."}


def api_collect(user: dict, payload: dict) -> dict[str, Any]:
    """Запустить сбор. Отвечаем сразу — прогон идёт в фоне."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    if _collecting.is_set():
        return {"error": "Сбор уже идёт"}

    def run() -> None:
        _collecting.set()
        try:
            import collector
            collector.run(days=int(os.environ.get("ADMETRICS_WINDOW_DAYS", "7")))
        except Exception:  # noqa: BLE001 — поток не должен ронять сервер
            import traceback
            traceback.print_exc()
        finally:
            _collecting.clear()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": "Сбор запущен — обновите через минуту."}


def api_identity_action(user: dict, payload: dict) -> dict[str, Any]:
    """Включить, выключить или отозвать личку."""
    ident_id = str(payload.get("id") or "")
    action = str(payload.get("action") or "")
    ident = store.get_identity(ident_id)
    if not ident:
        return {"error": "Личка не найдена"}
    if user["role"] != "admin" and ident["marketer_id"] != user.get("id"):
        return {"error": "Чужая личка"}
    if ident["status"] == "revoked":
        # Отзыв означает «секрет скомпрометирован». Тихо оживить такую
        # запись нельзя: она снова пошла бы в сбор со старым секретом.
        return {"error": "Секрет отозван, включать нечего"}
    if action == "enable":
        store.set_identity_enabled(ident_id, True)
        return {"ok": True, "message": "Личка включена"}
    if action == "disable":
        store.set_identity_enabled(ident_id, False)
        return {"ok": True, "message": "Личка выключена"}
    if action == "revoke":
        store.delete_identity(ident_id)
        return {"ok": True, "message": "Секрет удалён"}
    return {"error": "Неизвестное действие"}


def api_add_identity(user: dict, payload: dict) -> dict[str, Any]:
    """Завести новую личку маркетологу."""
    mid = str(payload.get("marketer_id") or "")
    if user["role"] != "admin" and user.get("id") != mid:
        return {"error": "Недоступно"}
    if not any(m["id"] == mid for m in store.list_marketers()):
        return {"error": "Маркетолог не найден"}
    secret, inline_proxy = split_secret_and_proxy(str(payload.get("secret") or ""))
    if not secret_is_complete(secret):
        return {"error": "Это не похоже на целый мультитокен: жду контейнер "
                         "FBTOOL или токен EAA."}
    typed, err = _proxy_in(str(payload.get("proxy") or inline_proxy))
    if err:
        return {"error": err}
    proxy = store.resolve_proxy(str(payload.get("proxy_id") or ""), typed,
                                None if user["role"] == "admin" else (user.get("id") or ""))
    name = str(payload.get("session_name") or "").strip()
    store.add_identity(mid, secret, proxy=proxy, session_name=name,
                       label=name or f"панель {datetime.now():%d.%m %H:%M}")
    return {"ok": True, "message": "Личка добавлена. Проверю на ближайшем сборе."}


def api_add_identity_bulk(user: dict, payload: dict) -> dict[str, Any]:
    """Массовое добавление мультитокенов — по одному на строку.

    Формат строки: «Имя сессии | <контейнер/EAA> [пробел прокси]».
    Имя и прокси необязательны. Общий прокси из выпадающего списка
    применяется к строкам без своего — обычно все личики на одном прокси.
    """
    mid = str(payload.get("marketer_id") or "")
    if user["role"] != "admin" and user.get("id") != mid:
        return {"error": "Недоступно"}
    if not any(m["id"] == mid for m in store.list_marketers()):
        return {"error": "Маркетолог не найден"}
    shared_proxy_id = str(payload.get("proxy_id") or "")
    added, failed = 0, []
    for i, raw in enumerate(str(payload.get("text") or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        name, rest = ("", line)
        if " | " in line:
            name, rest = line.split(" | ", 1)
        secret, inline_proxy = split_secret_and_proxy(rest.strip())
        if not secret_is_complete(secret):
            failed.append(name.strip() or f"строка {i}")
            continue
        typed, perr = _proxy_in(inline_proxy)
        if perr:
            failed.append((name.strip() or f"строка {i}") + f": {perr}")
            continue
        proxy = store.resolve_proxy(shared_proxy_id, typed,
                                    None if user["role"] == "admin" else (user.get("id") or ""))
        nm = name.strip()
        store.add_identity(mid, secret, proxy=proxy, session_name=nm,
                           label=nm or f"панель {datetime.now():%d.%m %H:%M}")
        added += 1
    msg = f"Добавлено личек: {added}"
    if failed:
        msg += f" · не распознано: {len(failed)}"
    return {"ok": True, "added": added, "failed": failed, "message": msg}


def api_rename_session(user: dict, payload: dict) -> dict[str, Any]:
    ident = store.get_identity(str(payload.get("id") or ""))
    if not ident:
        return {"error": "Личка не найдена"}
    if user["role"] != "admin" and ident["marketer_id"] != user.get("id"):
        return {"error": "Чужая личка"}
    store.set_session_name(ident["id"], str(payload.get("name") or ""))
    return {"ok": True, "message": "Имя сессии сохранено"}


def api_set_owner(user: dict, payload: dict) -> dict[str, Any]:
    """Закрепить кабинет за маркетологом с сегодняшнего дня."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    act_id = str(payload.get("act_id") or "")
    mid = str(payload.get("marketer_id") or "")
    if not store.get_ad_account(act_id):
        return {"error": "Кабинет не найден"}
    if not any(m["id"] == mid for m in store.list_marketers()):
        return {"error": "Маркетолог не найден"}
    # Прошлое не переписывается: спенд до сегодняшнего дня остаётся за
    # прежним владельцем, иначе история отчётов поехала бы задним числом.
    store.set_owner(act_id, mid, date.today().isoformat())
    return {"ok": True, "message": "Владелец назначен"}


def api_add_marketer(user: dict, payload: dict) -> dict[str, Any]:
    """Завести маркетолога и сразу выдать ссылку-приглашение."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 64:
        return {"error": "Имя не подошло"}
    try:
        mid = store.create_marketer(name, login=name.lower().replace(" ", "_"))
    except Exception as exc:  # noqa: BLE001 — обычно дубль логина
        return {"error": f"Не удалось создать: {exc}"[:180]}
    return {"ok": True, "id": mid, "invite": _invite_link(mid),
            "message": f"{name} заведён"}


def api_invite(user: dict, payload: dict) -> dict[str, Any]:
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    mid = str(payload.get("id") or "")
    if not any(m["id"] == mid for m in store.list_marketers()):
        return {"error": "Маркетолог не найден"}
    return {"ok": True, "invite": _invite_link(mid)}


def _invite_link(marketer_id: str) -> str:
    """Одноразовая ссылка входа. Тот же механизм, что у бота."""
    code = secrets.token_hex(4).upper()
    with store.connect() as conn:
        conn.execute(store._q("""
            INSERT INTO tg_link_codes (code, marketer_id, created_at)
            VALUES (?, ?, ?)
        """), (code, marketer_id, store.now()))
    username = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
    if not username:
        me = _bot_username()
        username = me or "bot"
    return f"https://t.me/{username}?start={code}"


_bot_username_cache: dict[str, str] = {}


def _bot_username() -> str:
    if "name" in _bot_username_cache:
        return _bot_username_cache["name"]
    try:
        import urllib.request
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=15) as r:
            name = json.load(r).get("result", {}).get("username", "")
    except Exception:  # noqa: BLE001 — без имени ссылка просто не соберётся
        name = ""
    _bot_username_cache["name"] = name
    return name


# --------------------------------------------------------------------------- #
# SMM-панель (smmlaba). Только админ: заказы тратят живой баланс панели.
# Ключ живёт в SMMLABA_API_KEY на сервере и во фронт не отдаётся.
# --------------------------------------------------------------------------- #
_SMM_CACHE: dict[str, Any] = {"at": 0.0, "services": None}
_SMM_TTL = 600  # каталог услуг большой и меняется редко — держим 10 минут


def _smm_services_cached() -> list[dict[str, Any]]:
    now_t = time.time()
    if _SMM_CACHE["services"] is None or now_t - _SMM_CACHE["at"] > _SMM_TTL:
        _SMM_CACHE["services"] = smm.Smm().services()
        _SMM_CACHE["at"] = now_t
    return _SMM_CACHE["services"] or []


def _smm_num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def api_smm(user: dict, params: dict) -> dict[str, Any]:
    """Баланс панели + каталог услуг (кэш). Только админ."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    if not os.environ.get("SMMLABA_API_KEY", "").strip():
        return {"error": "Ключ панели не задан: добавьте SMMLABA_API_KEY в .env"}
    try:
        bal = smm.Smm().balance()
        raw = _smm_services_cached()
    except smm.SmmError as exc:
        return {"error": f"Панель: {exc}"}
    services = [{
        "id": str(x.get("service")), "name": x.get("name") or "",
        "category": x.get("category") or "",
        "type": x.get("type") or "default",
        "rate": _smm_num(x.get("rate")),
        "min": int(_smm_num(x.get("min"))), "max": int(_smm_num(x.get("max"))),
        "dripfeed": bool(x.get("dripfeed")), "refill": bool(x.get("refill")),
        "cancel": bool(x.get("cancel")),
    } for x in raw]
    cats: dict[str, int] = {}
    for x in services:
        cats[x["category"]] = cats.get(x["category"], 0) + 1
    return {
        "balance": _smm_num(bal.get("balance")),
        "currency": bal.get("currency") or "",
        "categories": sorted(cats.keys()),
        "services": services,
    }


def api_smm_orders(user: dict, params: dict) -> dict[str, Any]:
    """История заказов. С ?refresh=1 сперва обновляет статусы незавершённых."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    if params.get("refresh") == "1" and os.environ.get("SMMLABA_API_KEY"):
        try:
            open_ids = store.smm_orders_open(100)
            if open_ids:
                res = smm.Smm().status_many(open_ids)
                if isinstance(res, dict):
                    for oid, st in res.items():
                        if isinstance(st, dict) and not st.get("error"):
                            store.update_smm_status(str(oid), st)
        except smm.SmmError:
            pass  # обновление статусов не должно ронять показ истории
    return {"orders": store.list_smm_orders(300)}


def api_smm_order(user: dict, payload: dict) -> dict[str, Any]:
    """Создать заказ. Тратит баланс панели — только явным действием админа."""
    if user["role"] != "admin":
        return {"error": "Недоступно"}
    service = str(payload.get("service") or "").strip()
    link = str(payload.get("link") or "").strip()
    quantity = payload.get("quantity")
    if not service or not link:
        return {"error": "Нужны услуга и ссылка"}
    svc = next((x for x in _smm_services_cached()
                if str(x.get("service")) == service), None)
    try:
        qty = int(quantity) if quantity not in (None, "") else None
    except (TypeError, ValueError):
        return {"error": "Количество должно быть числом"}
    if svc and qty is not None:
        lo, hi = int(_smm_num(svc.get("min"))), int(_smm_num(svc.get("max")))
        if lo and qty < lo:
            return {"error": f"Минимум для этой услуги — {lo}"}
        if hi and qty > hi:
            return {"error": f"Максимум для этой услуги — {hi}"}
    extra = {k: payload[k] for k in ("runs", "interval", "comments",
             "username", "hashtags", "usernames") if payload.get(k)}
    try:
        res = smm.Smm().add(service, link, qty, **extra)
    except smm.SmmError as exc:
        return {"error": f"Панель: {exc}"}
    oid = res.get("order") if isinstance(res, dict) else None
    if not oid:
        return {"error": f"Панель не вернула номер заказа: {res}"}
    store.save_smm_order({
        "order_id": str(oid), "service_id": service,
        "service_name": (svc or {}).get("name", ""),
        "category": (svc or {}).get("category", ""),
        "link": link, "quantity": qty or 0,
        "charge": round(_smm_num((svc or {}).get("rate")) * (qty or 0) / 1000, 4)
        if svc else 0,
        "currency": "", "status": "Pending",
        "marketer_id": user.get("id"),
    })
    # Сразу подтянуть реальную цену/стартовый счётчик от панели.
    try:
        store.update_smm_status(str(oid), smm.Smm().status(oid))
    except smm.SmmError:
        pass
    return {"ok": True, "order": str(oid), "message": f"Заказ #{oid} создан."}


POST_ROUTES = {
    "/api/identity/retoken": api_retoken,
    "/api/identity/action": api_identity_action,
    "/api/identity/add": api_add_identity,
    "/api/identity/add_bulk": api_add_identity_bulk,
    "/api/identity/rename": api_rename_session,
    "/api/collect": api_collect,
    "/api/account/owner": api_set_owner,
    "/api/marketer/add": api_add_marketer,
    "/api/marketer/invite": api_invite,
    "/api/comments/scan": api_comments_scan,
    "/api/comments/clean": api_comments_clean,
    "/api/comments/auto": api_comments_auto,
    "/api/comments/auto_all": api_comments_auto_all,
    "/api/comments/clean_all": api_comments_clean_all,
    "/api/proxy/add": api_proxy_add,
    "/api/proxy/check": api_proxy_check,
    "/api/proxy/delete": api_proxy_delete,
    "/api/proxy/probe": api_proxy_probe,
    "/api/proxy/setdns": api_proxy_setdns,
    "/api/proxy/rename": api_proxy_rename,
    "/api/identity/proxy": api_identity_proxy,
    "/api/fx/set": api_fx_set,
    # Единственные ручки, которые что-то меняют у Meta. Подтверждение
    # спрашивает интерфейс; журнал пишется здесь, и на успех, и на отказ.
    "/api/keys/create": api_keys_create,
    "/api/keys/revoke": api_keys_revoke,
    "/api/campaign/action": api_campaign_action,
    "/api/smm/order": api_smm_order,
    "/api/adset/action": api_adset_action,
}

# Раздел «Telegram»: технические аккаунты. Ручки живут в tg_panel.py.
import tg_panel  # noqa: E402
POST_ROUTES.update(tg_panel.POST_ROUTES)

# Идёт ли сейчас сбор — чтобы не запускать второй поверх первого.
_collecting = threading.Event()


ROUTES = {
    "/api/summary": api_summary,
    "/api/campaigns": api_campaigns,
    "/api/adset": api_adset,
    "/api/campaign": api_campaign,
    "/api/keys": api_keys_list,
    "/api/marketers": api_marketers,
    "/api/alerts": api_alerts,
    "/api/health": api_health,
    "/api/recon": api_recon,
    "/api/comments": api_comments,
    "/api/account/recovery": api_recovery,
    "/api/fx": api_fx_get,
    "/api/proxies": api_proxies,
    "/api/proxy/list": api_proxy_list,
    "/api/links/export": api_links_export,
    "/api/creatives": api_creatives,
    "/api/recovery/search": api_recovery_search,
    "/api/recovery/one": api_recovery_one,
    "/api/accounts": api_accounts,
    "/api/account": api_account,
    "/api/identities": api_identities,
    "/api/identity": api_identity,
    "/api/businesses": api_businesses,
    "/api/cards": api_cards,
    "/api/smm": api_smm,
    "/api/smm/orders": api_smm_orders,
}
ROUTES.update(tg_panel.GET_ROUTES)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "Admetrics"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Свой формат: штатный печатает каждую картинку и топит полезное.
        if "/api/" in str(args[0] if args else ""):
            print(f"[{datetime.now():%H:%M:%S}] {args[0]}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str,
              extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Панель живёт внутри Telegram, встраивать её куда-то ещё незачем.
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for name, value in (extra or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where: str,
                  extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        for name, value in (extra or []):
            self.send_header(name, value)
        self.end_headers()

    def _cookie_header(self, value: str, max_age: int) -> tuple[str, str]:
        """Кука сессии. HttpOnly — чтобы её не достал скрипт на странице;
        Secure — чтобы не ушла по http; SameSite=Lax — чтобы её не приложили
        к запросу, инициированному чужим сайтом."""
        return ("Set-Cookie",
                f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; "
                f"HttpOnly; Secure; SameSite=Lax")

    def _local_admin(self) -> dict[str, Any] | None:
        """Доступ админа для запросов с этой же машины.

        Нужен, чтобы панель открывалась в обычном браузере по
        http://127.0.0.1:8080 — без Telegram, без туннеля и без сертификата.
        Подпись `initData` умеет ставить только Telegram, поэтому вне него
        панель иначе отвечает 401 на каждый запрос.

        Три условия сразу, и каждое обязательно:
      • включено явным флагом ADMETRICS_LOCAL_ADMIN=1 — по умолчанию выключено;
      • соединение пришло с 127.0.0.1;
      • в запросе нет заголовков X-Forwarded-* / X-Real-IP.

        Последнее — не паранойя. Если панель однажды окажется за обратным
        прокси, тот будет подключаться как раз с 127.0.0.1, и без этой
        проверки правами админа обладал бы весь интернет.
        """
        if os.environ.get("ADMETRICS_LOCAL_ADMIN", "").strip() != "1":
            return None
        host = (self.client_address[0] if self.client_address else "")
        if host not in ("127.0.0.1", "::1"):
            return None
        for name in self.headers:
            low = name.lower()
            if low.startswith("x-forwarded") or low == "x-real-ip":
                return None
        # Заголовок Host — единственный надёжный признак. Туннель подключается
        # к 127.0.0.1, то есть по адресу соединения он неотличим от браузера
        # на этой машине, а X-Forwarded-For ставит не всякий. Но в Host при
        # заходе через туннель стоит его публичное имя, а не loopback.
        # Без этой проверки права админа получал бы любой, кто знает адрес
        # туннеля, — что и произошло.
        seen = (self.headers.get("Host") or "").split(":")[0].lower()
        if seen not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return None
        return {"id": None, "name": "Локальный админ", "role": "admin",
                "tg_id": "local"}

    def _cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for chunk in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = chunk.strip().partition("=")
            if name:
                out[name] = value
        return out

    def _session_user(self) -> str | None:
        return read_session(self._cookies().get(SESSION_COOKIE, ""))

    def _auth(self) -> dict[str, Any] | None:
        """Кто спрашивает. None — ответ уже отправлен, обработку прекратить.

        Подпись проверяется на КАЖДЫЙ запрос: без неё достаточно было бы
        подставить чужой user_id в заголовок и получить чужие цифры.
        """
        local = self._local_admin()
        if local:
            return local
        # Порядок важен: Mini App присылает подпись на каждый запрос, портал —
        # куку. Проверяем подпись первой, чтобы открытая в Telegram панель
        # не зависела от чужой сессии, случайно оставшейся в том же браузере.
        tg_user = check_init_data(self.headers.get("X-Telegram-Init-Data", ""))
        tg_id = tg_user.get("id") if tg_user else self._session_user()
        if not tg_id:
            self._send(401, json.dumps(
                {"error": "Нужно войти", "login": "/login"},
                ensure_ascii=False).encode(),
                "application/json; charset=utf-8")
            return None
        user = whoami(tg_id)
        if user["role"] == "guest":
            self._send(403, json.dumps(
                {"error": "Нет доступа. Попросите ссылку у администратора."},
                ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return None
        return user

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        handler = POST_ROUTES.get(parsed.path)
        if not handler:
            return self._send(404, b'{"error":"not found"}', "application/json")
        user = self._auth()
        if user is None:
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            # Ограничение на размер: сюда кладут контейнер, а не файл.
            body = json.loads(self.rfile.read(min(size, 512 * 1024)) or b"{}")
            payload = jsonable(handler(user, body))
        except Exception as exc:  # noqa: BLE001 — панель не должна падать молча
            import traceback
            traceback.print_exc()
            payload = {"error": str(exc)[:200]}
        self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _api_key(self) -> dict[str, Any] | None:
        """Ключ из заголовка. None — ответ уже отправлен.

        Только `Authorization: Bearer`. Ключ в query-параметре не принимаем
        намеренно: URL попадает в логи прокси, в историю браузера и в
        referer, и ключ оттуда работает так же, как из заголовка.
        """
        raw = self.headers.get("Authorization", "")
        if not raw.lower().startswith("bearer "):
            self._send(401, json.dumps(
                {"error": "Нужен заголовок Authorization: Bearer <ключ>"},
                ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return None
        ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or self.client_address[0])
        key = store.api_key_by_secret(raw[7:].strip(), ip)
        if not key:
            self._send(403, json.dumps(
                {"error": "Ключ не найден или отозван"},
                ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return None
        return key

    def _serve_api_v1(self, parsed: Any, params: dict) -> None:
        name = parsed.path[len(API_PREFIX):].strip("/")
        handler = API_V1.get(name)
        if not handler:
            return self._send(404, json.dumps(
                {"error": "Нет такой ручки", "available": sorted(API_V1)},
                ensure_ascii=False).encode(), "application/json; charset=utf-8")
        key = self._api_key()
        if key is None:
            return
        try:
            payload = jsonable(handler(key, params))
        except Exception as exc:  # noqa: BLE001 — наружу не отдаём стектрейс
            traceback.print_exc()
            payload = {"error": str(exc)[:200]}
        code = 400 if payload.get("error") else 200
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in
                  urllib.parse.parse_qs(parsed.query).items()}

        # Внешнее API идёт своей дорогой: у него ключ вместо сессии и свой
        # набор ручек. Проверять его сессионным `_auth` нельзя — тот в ответ
        # на отсутствие куки шлёт человека на страницу входа.
        if parsed.path.startswith(API_PREFIX):
            return self._serve_api_v1(parsed, params)

        if parsed.path in ("/", "/index.html"):
            # Портал в браузере без сессии показывать нечего: экран нарисуется,
            # а все ручки ответят 401. Отправляем на вход сразу. Внутри
            # Telegram куки нет и не будет — там подпись едет заголовком,
            # поэтому Mini App узнаём по параметру, который ставит сам клиент.
            if not (self._session_user() or params.get("tgWebApp")
                    or self._local_admin()):
                return self._redirect("/login")
            page = (HERE / "webapp" / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")

        if parsed.path == "/login":
            return self._send(200, login_page(), "text/html; charset=utf-8")

        if parsed.path == "/auth/telegram":
            data = check_login_widget(params)
            if not data:
                return self._send(
                    403, login_page("Подпись не сошлась или устарела. "
                                    "Попробуйте войти заново."),
                    "text/html; charset=utf-8")
            person = whoami(data["id"])
            if person["role"] == "guest":
                return self._send(
                    403, login_page(
                        f"Вы вошли как {ui.esc(data.get('first_name') or '')}, "
                        "но доступа нет. Попросите ссылку-приглашение "
                        "у администратора и откройте её в Telegram."),
                    "text/html; charset=utf-8")
            return self._redirect("/", [self._cookie_header(
                make_session(data["id"]), SESSION_MAX_AGE)])

        if parsed.path == "/logout":
            # Пустая кука с нулевым сроком — единственный способ убрать её
            # у браузера: удалять куки сервер не умеет, только перезаписывать.
            return self._redirect("/login", [self._cookie_header("", 0)])

        # Аватарка профиля из Telegram. /api/avatar/tg/<id> — по tg-id,
        # /api/avatar/m/<marketer_id> — сервер сам достаёт tg-id маркетолога.
        # Нет фото или закрыта приватность → 404, фронт покажет свой кружок.
        if parsed.path.startswith("/api/avatar/"):
            # Без проверки сессии намеренно: в Telegram Mini App куки нет,
            # авторизация едет заголовком, а <img> заголовки не шлёт — с
            # проверкой аватарки в Telegram не грузились бы вовсе. Ручка
            # отдаёт только кэшированное фото профиля из TG, ничего больше.
            rest = parsed.path[len("/api/avatar/"):]
            tg = ""
            if rest.startswith("tg/"):
                tg = rest[3:]
            elif rest.startswith("m/"):
                tg = store.marketer_tg(rest[2:])
            data = avatars.avatar_bytes(tg, TOKEN) if tg.isdigit() else None
            if not data:
                return self._send(404, b"", "image/jpeg",
                                  [("Cache-Control", "public, max-age=3600")])
            return self._send(200, data, "image/jpeg",
                              [("Cache-Control", "private, max-age=86400")])

        handler = ROUTES.get(parsed.path)
        if not handler:
            return self._send(404, b'{"error":"not found"}', "application/json")

        user = self._auth()
        if user is None:
            return

        try:
            payload = jsonable(handler(user, params))
        except Exception as exc:  # noqa: BLE001 — панель не должна падать молча
            import traceback
            traceback.print_exc()
            payload = {"error": str(exc)[:200]}
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(200, body, "application/json; charset=utf-8")


# --------------------------------------------------------------------------- #
# Туннель
# --------------------------------------------------------------------------- #
def start_tunnel(port: int) -> str:
    """Поднять HTTPS-туннель cloudflared и вернуть публичный адрес.

    Telegram открывает Mini App только по https, а сертификата у локальной
    машины нет. Быстрый туннель выдаёт временный домен — он меняется при
    каждом запуске, поэтому адрес пишется в файл, откуда его берёт бот.
    """
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1)
    url = ""
    for line in proc.stdout:  # type: ignore[union-attr]
        found = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if found:
            url = found.group(0)
            break
    if not url:
        raise RuntimeError("cloudflared не отдал адрес")

    def drain() -> None:
        for _ in proc.stdout:  # type: ignore[union-attr]
            pass

    threading.Thread(target=drain, daemon=True).start()
    URL_FILE.write_text(url, encoding="utf-8")
    return url


def main() -> int:
    ap = argparse.ArgumentParser(description="Панель Admetrics для Telegram")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tunnel", action="store_true",
                    help="поднять HTTPS-туннель cloudflared")
    args = ap.parse_args()

    if not TOKEN:
        print("Не задан TELEGRAM_BOT_TOKEN — подпись проверять нечем",
              file=sys.stderr)
        return 2
    store.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Панель на http://127.0.0.1:{args.port}")

    if args.tunnel:
        url = start_tunnel(args.port)
        print(f"Публичный адрес: {url}")
        print(f"Записан в {URL_FILE.name} — бот покажет кнопку сам.")
        print("ВНИМАНИЕ: --tunnel не следит за туннелем. Если cloudflared "
              "отвалится, кнопка в боте останется и приведёт в Error 1033.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        # Адрес живёт ровно столько, сколько живёт процесс. Иначе бот рисует
        # кнопку в мёртвый туннель — именно так и получался Error 1033.
        if args.tunnel:
            URL_FILE.write_text("", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
