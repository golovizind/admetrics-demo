"""
Синхронизация реестра аккаунтов из Google-таблицы.

Таблица — источник данных восстановления: логины, пароли, 2FA, резервная
почта, ссылка на профиль. Ключ связки с нашими данными — «Сессия» (имя
сессии), совпадающее с антидетектом и с `identities.session_name`.

Чувствительные поля не хранятся открытыми: они шифруются в `account_recovery`
тем же ключом, что и мультитокены, и отдаются только по явному запросу
владельца или админа.

URL листа — в .env (SHEET_CSV_URL). Лист должен быть открыт на чтение
«по ссылке», иначе CSV-экспорт вернёт 401.
"""

from __future__ import annotations

import csv
import io
import os

import requests

from . import store

# Экспорт конкретной вкладки как CSV задаётся в .env (SHEET_CSV_URL), вида
# https://docs.google.com/spreadsheets/d/<SHEET_ID>/export?format=csv&gid=<GID>
DEFAULT_URL = ""

# Позиции колонок в листе. Держим здесь, чтобы перестановка в таблице
# чинилась одним местом, а не по всему коду.
COL = {"session": 0, "farmer": 1, "account": 2, "dob": 3, "phone": 4,
       "login": 5, "pass_account": 6, "pass_email": 7, "reserve_email": 8,
       "reserve_email_pass": 9, "twofa": 10, "fb_link": 11}


def _clean(value: str) -> str:
    value = (value or "").strip()
    return "" if value in ("-", "—", "") else value


def sync_recovery(url: str = "") -> dict[str, int]:
    """Скачать лист и обновить данные восстановления. Возвращает счётчики."""
    url = url or os.environ.get("SHEET_CSV_URL", "").strip() or DEFAULT_URL
    if not url:
        raise RuntimeError("не задан SHEET_CSV_URL в .env")
    r = requests.get(url, timeout=40)
    r.raise_for_status()
    if r.text[:40].lower().count("<html") or r.status_code != 200:
        raise RuntimeError("лист недоступен — откройте доступ по ссылке на чтение")

    rows = list(csv.reader(io.StringIO(r.text)))
    written, skipped = 0, 0
    for row in rows[1:]:
        if len(row) <= COL["fb_link"]:
            skipped += 1
            continue
        session = _clean(row[COL["session"]])
        if not session:
            skipped += 1
            continue
        secret = {
            "pass_account": _clean(row[COL["pass_account"]]),
            "pass_email": _clean(row[COL["pass_email"]]),
            "reserve_email": _clean(row[COL["reserve_email"]]),
            "reserve_email_pass": _clean(row[COL["reserve_email_pass"]]),
            "twofa": _clean(row[COL["twofa"]]),
            "dob": _clean(row[COL["dob"]]),
            "phone": _clean(row[COL["phone"]]),
        }
        store.upsert_recovery(
            session, _clean(row[COL["farmer"]]), _clean(row[COL["account"]]),
            _clean(row[COL["fb_link"]]), _clean(row[COL["login"]]), secret)
        written += 1
    return {"written": written, "skipped": skipped, "rows": len(rows) - 1}
