#!/usr/bin/env python3
"""Разовая вытяжка ИСТОРИИ платежей из Brocard — глубже обычного окна.

Обычный сбор берёт последние 45 дней, поэтому «за всё время» на портале —
это лишь то, что успели накопить. Чтобы подтянуть настоящую историю, запусти
разово с нужной глубиной (в днях):

    cd /opt/admetrics && set -a && . ./.env && set +a && \\
      .venv/bin/python backfill_cards.py 400

Тянет ТОЛЬКО карты, Facebook не трогает. Платежи перезаписываются по
payment_id, так что повторный запуск не плодит дублей. Число — сколько дней
назад копать (по умолчанию 365).
"""
from __future__ import annotations

import sys

import collector


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    print(f"Тяну платежи Brocard за последние {days} дней…")
    run_id = collector.start_run(f"backfill-cards:{days}d")
    try:
        stats = collector.collect_cards(run_id, days)
        collector.finish_run(run_id, "ok", stats)
    except Exception as exc:  # noqa: BLE001
        collector.finish_run(run_id, "failed", {"error": str(exc)})
        print(f"Не удалось: {exc}")
        return 1
    print(f"Готово: платежей {stats.get('card_payments', 0)}, "
          f"карт {stats.get('cards_seen', 0)}, ошибок {stats.get('errors', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
