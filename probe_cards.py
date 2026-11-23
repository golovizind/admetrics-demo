#!/usr/bin/env python3
"""Живой ли ключ MyBroCards на сервере. Только чтение, ключ не печатается.

    cd /opt/admetrics && set -a && . ./.env && set +a && .venv/bin/python probe_cards.py

Ключ у клиента идёт в заголовке Authorization, а не в URL, поэтому текст
ошибки печатать безопасно — ключа в нём нет.
"""
from __future__ import annotations

import os

from core import cards


def main() -> int:
    k = os.environ.get("MYBROCARD_API_KEY", "").strip()
    if not k:
        print("Ключ MYBROCARD_API_KEY в .env НЕ задан — карты сейчас не собираются.")
        return 1
    print(f"Ключ задан (длина {len(k)}), проверяю у MyBroCards…")
    try:
        api = cards.MyBroCards()
        since, until = cards.default_window(2)   # узкое окно — лёгкий запрос
        pay = api.fetch_payments(since, until)
        print(f"ОК: ключ живой. Платежей за {since}…{until}: {len(pay)}.")
        return 0
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:200]
        print(f"ОШИБКА: {msg}")
        if msg.startswith("401") or "403" in msg[:4]:
            print("→ ключ не принят: отозван или другой скоуп. Нужен новый.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
