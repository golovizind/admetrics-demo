"""
Циклы залива — недели пн–вс со сквозной нумерацией.

Модель взята из SpendTool без изменений: номер цикла не хранится в базе,
а вычисляется из даты через якорь. Это дёшево и не рассинхронизируется —
нет второго источника правды, который может разойтись с датами фактов.

Якорь команды: понедельник 2026-08-03 — это цикл 239. Менять якорь нельзя:
вся историческая нумерация в разговоре команды сдвинется.

Циклы — первичное измерение отчётности (решение по иерархии фаундера
«маркетолог → цикл → гео»). Календарные месяцы остаются доступны как
альтернативная группировка, но экраны по умолчанию строятся на циклах.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# Якорь нумерации: эта дата — понедельник и начало цикла ANCHOR_CYCLE.
ANCHOR_DATE = date(2026, 8, 3)
ANCHOR_CYCLE = 239

_CYCLE_RE = re.compile(r"^\s*[cCцЦ]?\s*(\d{1,5})\s*$")


def _monday_of(day: date) -> date:
    """Понедельник недели, в которую попадает day."""
    return day - timedelta(days=day.weekday())


def cycle_of(day: date) -> int:
    """Номер цикла, в который попадает дата.

    Считается по разнице недель от якоря. Деление целочисленное и работает
    в обе стороны: для дат раньше якоря номера уходят вниз корректно,
    потому что разница дней всегда кратна 7 после приведения к понедельнику.
    """
    delta_days = (_monday_of(day) - ANCHOR_DATE).days
    return ANCHOR_CYCLE + delta_days // 7


def cycle_range(cycle: int) -> tuple[date, date]:
    """Границы цикла включительно: (понедельник, воскресенье)."""
    start = ANCHOR_DATE + timedelta(weeks=cycle - ANCHOR_CYCLE)
    return start, start + timedelta(days=6)


def cycle_bounds_iso(cycle: int) -> tuple[str, str]:
    """То же, но строками 'YYYY-MM-DD' — в таком виде даты лежат в базе."""
    start, end = cycle_range(cycle)
    return start.isoformat(), end.isoformat()


def current_cycle(today: date | None = None) -> int:
    return cycle_of(today or date.today())


def cycles_between(since: date, until: date) -> list[int]:
    """Все циклы, которые пересекаются с периодом, по возрастанию."""
    if until < since:
        since, until = until, since
    return list(range(cycle_of(since), cycle_of(until) + 1))


def last_cycles(count: int, today: date | None = None) -> list[int]:
    """Последние `count` циклов, включая текущий, по возрастанию."""
    if count < 1:
        return []
    now = current_cycle(today)
    return list(range(now - count + 1, now + 1))


def parse_cycle(value: str | int, today: date | None = None) -> int:
    """Разбор пользовательского ввода: 240, '240', 'c240', 'ц240',
    а также относительных 'current' / 'prev'. Нужен для команд бота."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text in {"current", "now", "текущий", "тек"}:
        return current_cycle(today)
    if text in {"prev", "previous", "прошлый", "пред"}:
        return current_cycle(today) - 1
    match = _CYCLE_RE.match(text)
    if not match:
        raise ValueError(f"не похоже на номер цикла: {value!r}")
    return int(match.group(1))


def label(cycle: int) -> str:
    """Человеческая подпись для интерфейса: 'Цикл 240 · 10–16 авг'."""
    start, end = cycle_range(cycle)
    months = ("янв", "фев", "мар", "апр", "мая", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек")
    if start.month == end.month:
        span = f"{start.day}–{end.day} {months[end.month - 1]}"
    else:
        span = (f"{start.day} {months[start.month - 1]} – "
                f"{end.day} {months[end.month - 1]}")
    return f"Цикл {cycle} · {span}"
