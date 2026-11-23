"""
Экраны Telegram-бота: текст + клавиатура.

Отделено от транспорта намеренно. Каждая функция `screen_*` возвращает пару
`(текст, клавиатура)` и ничего не отправляет: транспорт сам решает, послать
новое сообщение или заменить прежнее.

Управление в боте только кнопочное — команд нет вовсе. Поэтому экран обязан
нести все свои действия в клавиатуре: то, чего нет на кнопке, недоступно
никак. Это же и защита: `callback_data` приходит от клиента, так что каждое
действие ещё раз проверяется на стороне бота.

Про `callback_data`: Telegram даёт на неё **64 байта**, поэтому коды действий
короткие (`sp:7d`, `a:act_123`), а не человекочитаемые. Длинные значения вроде
названий в неё не кладутся никогда.
"""

from __future__ import annotations

import html
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from . import alerts, cycles, store
from .schema import BASE_CURRENCY

PAGE_SIZE = 8


# --------------------------------------------------------------------------- #
# Мелочи вёрстки
# --------------------------------------------------------------------------- #
def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def money(value: Any) -> str:
    return f"{Decimal(value or 0):,.2f}".replace(",", " ")


# Адрес панели пишет `webapp.py` при старте туннеля: быстрый туннель выдаёт
# новый домен на каждый запуск, поэтому зашить его в настройки нельзя.
WEBAPP_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", ".webapp_url")


def webapp_url() -> str:
    url = os.environ.get("WEBAPP_URL", "").strip()
    if url:
        return url
    try:
        with open(WEBAPP_URL_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# Кеш живости адреса: проверять сеть на каждую отрисовку экрана нельзя.
# Отрицательный результат держим меньше положительного: туннель поднимается
# позже бота, и с общим TTL кнопка ждала бы минуту после того, как всё готово.
_ALIVE: dict[str, Any] = {"url": "", "ok": False, "at": 0.0, "why": ""}


_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек")


def last_page(total: int) -> int:
    return max(0, (total - 1) // PAGE_SIZE)


PERIODS = [("Сегодня", "today"), ("7 дней", "7d"), ("30 дней", "30d")]


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #
def accounts_of(marketer_id: str) -> list[dict[str, Any]]:
    """Кабинеты, которыми маркетолог владеет СЕГОДНЯ.

    Условие обязано совпадать с `store.owner_at` слово в слово. Иначе
    список и карточка расходятся: кабинет виден в перечне, а по нажатию
    отвечает «это не ваш кабинет» — и наоборот.
    """
    today = date.today().isoformat()
    with store.connect() as conn:
        return store.rows(conn.execute(store._q("""
            SELECT a.* FROM ad_accounts a
              JOIN ad_account_owner o ON o.act_id = a.act_id
                   AND o.since <= ? AND (o.until IS NULL OR o.until >= ?)
             WHERE o.marketer_id = ?
             ORDER BY a.account_status, a.amount_spent DESC
        """), (today, today, marketer_id)))


def owns_account(marketer_id: str, act_id: str) -> bool:
    """Проверка владения перед показом кабинета.

    `act_id` приходит из callback_data, то есть его можно подделать, отправив
    боту произвольный запрос. Ограничение обязано быть на сервере, а не в том,
    что мы не нарисовали кнопку.
    """
    return store.owner_at(act_id, date.today().isoformat()) == marketer_id


def last_run() -> dict[str, Any] | None:
    with store.connect() as conn:
        found = store.rows(conn.execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT 1"))
    return found[0] if found else None


BREAKDOWN_TITLES = [("age", "Возраст"), ("gender", "Пол"),
                    ("platform", "Площадка"), ("device", "Устройство"),
                    ("region", "Регион")]


# Роли в BM и статус проверки страницы. Переводим только известные:
# Meta заводит новые роли молча, и подставлять для них выдуманное слово
# опаснее, чем показать константу как есть.
BM_ROLES = {
    "ADMIN": "админ", "EMPLOYEE": "сотрудник", "DEVELOPER": "разработчик",
    "FINANCE_EDITOR": "финансы, полный доступ",
    "FINANCE_ANALYST": "финансы, только чтение",
    "ADS_RIGHTS_REVIEWER": "проверка прав на рекламу",
}
PAGE_VERIFY = {
    "VERIFIED": "подтверждена", "NOT_VERIFIED": "не подтверждена",
    "EXPIRED": "проверка истекла", "PENDING": "на проверке",
    "BLUE_VERIFIED": "синяя галочка", "GRAY_VERIFIED": "серая галочка",
}


def _visible_alerts(user: dict, limit: int = 40) -> list[dict[str, Any]]:
    """События, которые этому человеку положено видеть.

    Маркетологу — про него и его кабинеты. Фильтр здесь, а не в вёрстке:
    экранов, показывающих события, уже два.
    """
    found = alerts.recent(limit)
    if user["role"] == "admin":
        return found
    mine = {a["act_id"] for a in accounts_of(user["id"])} if user.get("id") else set()
    return [r for r in found
            if r["marketer_id"] == user.get("id") or r["act_id"] in mine]


# --------------------------------------------------------------------------- #
# Период — общий разбор для команд и кнопок
# --------------------------------------------------------------------------- #
def _num(text: str, lo: int, hi: int) -> int | None:
    """Строгое целое в диапазоне.

    `str.isdigit()` для этого не годится: он пропускает '²' и прочие
    юникод-цифры, на которых `int()` падает. Период приходит и из
    `callback_data`, и из команды — то есть его пишет клиент.
    """
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    return value if lo <= value <= hi else None


MAX_DAYS = 3650


def _iso_day(value: str) -> str:
    """'2026-09-01' → она же; мусор → пусто. Ничего не достраиваем: дата,
    угаданная из половины строки, опаснее отказа — по ней посчитают деньги."""
    import re as _re
    v = (value or "").strip()
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return ""
    try:
        date.fromisoformat(v)
    except ValueError:
        return ""
    return v


def _short_day(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.day} {_MONTHS[d.month - 1]}"


def parse_period(arg: str = "") -> tuple[str, str, str]:
    """'today' | '7d' | 'c240' | 'c238-240' → (since, until, подпись).

    Неразобранное значение — не ошибка: отдаём текущий цикл. Бот от кривой
    строки падать не должен, а «показал не то» здесь видно по подписи.
    """
    from datetime import timedelta
    arg = (arg or "").strip().lower()
    today = date.today()
    if not arg or arg in ("today", "сегодня"):
        return today.isoformat(), today.isoformat(), "сегодня"
    if arg in ("yesterday", "вчера"):
        d = (today - timedelta(days=1)).isoformat()
        return d, d, "вчера"
    if arg in ("all", "все"):
        return "2020-01-01", today.isoformat(), "за всё время"
    # Свой период: «2026-09-01..2026-09-30». Две точки, а не дефис — дефис
    # уже занят внутри самих дат, и «2026-09-01-2026-09-30» не разобрать
    # однозначно ни человеку, ни коду.
    if ".." in arg:
        head, _, tail = arg.partition("..")
        a, b = _iso_day(head), _iso_day(tail)
        if a and b:
            if a > b:
                a, b = b, a
            # Верхнюю границу не подрезаем сегодняшним днём: спрашивать
            # данные вперёд бессмысленно, но врать про заданный период
            # ещё хуже — пусть подпись говорит ровно то, что спросили.
            return a, b, f"{_short_day(a)} — {_short_day(b)}"
    if arg.endswith("d"):
        days = _num(arg[:-1], 1, MAX_DAYS)
        if days is not None:
            return ((today - timedelta(days=days - 1)).isoformat(),
                    today.isoformat(), f"{days} дн.")
    if arg.startswith(("c", "ц")):
        body = arg[1:]
        if "-" in body:
            head, _, tail = body.partition("-")
            a, b = _num(head, 1, 99999), _num(tail, 1, 99999)
            if a is not None and b is not None:
                a, b = min(a, b), max(a, b)
                return (cycles.cycle_bounds_iso(a)[0],
                        cycles.cycle_bounds_iso(b)[1], f"циклы {a}–{b}")
        n = _num(body, 1, 99999)
        if n is not None:
            since, until = cycles.cycle_bounds_iso(n)
            return since, until, cycles.label(n)
    cur = cycles.current_cycle()
    since, until = cycles.cycle_bounds_iso(cur)
    return since, until, cycles.label(cur)

