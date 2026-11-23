"""
MyBroCards: карты и платежи для сверки с открутом FB.

Зачем сверка. Meta показывает, сколько кабинет открутил, а карта — сколько
денег реально ушло. Расхождение между этими числами означает либо чужой
открут на карте, либо потерянный кабинет, либо проблему с биллингом. Ни
одно из этих событий по отдельным отчётам не видно.

Что здесь учтено — всё проверено на живых данных аккаунта:

  • **Отклонённые не деньги.** `state = Declined` — попытка списания, она
    не ушла. В зачёт не идёт, но комиссия за отказ ($0.5) — реальный расход,
    поэтому хранится отдельно.
  • **Комиссии лежат вне суммы.** `fees` — отдельный список, и сравнивать
    с FB нужно `amount` без них: иначе расхождение появлялось бы всегда,
    ровно на размер комиссии (1.1% + $0.3 на не-США).
  • **Не только Facebook.** В выборке есть TikTok. Платформа определяется
    по `merchant.name` и сохраняется, но в сверку с Meta идёт только FB.
  • **Мультивалютность.** Встречается `92.91 EUR → 107.48 USD`: списание
    в долларах, исходная сумма в евро. Сверяем по `amount`, то есть по
    тому, что фактически списано.
  • **Фильтр по карте на их стороне не работает** — параметр `card_id`
    молча игнорируется, ответ приходит по всем картам. Поэтому тянем по
    датам и раскладываем у себя.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator

import requests

BASE_URL = os.environ.get("MYBROCARD_BASE_URL",
                          "https://private.mybrocard.com/api/v2").rstrip("/")
PAGE_SIZE = 500
TIMEOUT = 60
MAX_RETRIES = 3

# Как их метки состояния ложатся на наши. В зачёт идут только settled
# и pending: первое — деньги ушли, второе — уйдут почти наверняка.
STATE_MAP = {
    "settled": "settled", "completed": "settled", "reversed": "reversed",
    "pending": "pending", "declined": "declined", "cancelled": "cancelled",
    # `void` в выборке встречается сотнями: авторизация снята, деньги
    # не ушли. В зачёт не идёт — наравне с отклонёнными.
    "void": "void",
}


class CardsError(RuntimeError):
    """Их API не ответил или ответил не тем."""


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001 — мусор в деньгах считаем нулём
        return Decimal("0")


class MyBroCards:
    def __init__(self, api_key: str = "", base_url: str = ""):
        self.key = (api_key or os.environ.get("MYBROCARD_API_KEY", "")).strip()
        self.base = (base_url or BASE_URL).rstrip("/")
        self.calls = 0
        if not self.key:
            raise CardsError("Не задан MYBROCARD_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base}/{path.lstrip('/')}"
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self.calls += 1
            try:
                r = self.session.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last = CardsError(f"сеть: {exc}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                # Их лимит: подождать и повторить, а не ронять весь прогон.
                time.sleep(5 * (attempt + 1))
                continue
            if not r.ok:
                # Ключ в заголовке, а не в URL, поэтому в текст ошибки он
                # не попадает — печатать ответ безопасно.
                raise CardsError(f"{r.status_code}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError as exc:
                raise CardsError(f"не JSON: {r.text[:200]}") from exc
        raise last or CardsError("не удалось получить ответ")

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict]:
        """Постранично, пока страницы не кончатся.

        Ограничение сверху обязательно: при сбое пагинации на их стороне
        цикл иначе идёт бесконечно, выжигая лимиты.
        """
        page = 1
        while page <= 200:
            payload = self._get(path, {**params, "page": page,
                                       "per_page": PAGE_SIZE})
            rows = payload.get("data") or []
            for row in rows:
                yield row
            last_page = int(payload.get("last_page") or 1)
            if page >= last_page or not rows:
                return
            page += 1

    # ---------------------------------------------------------------- #
    def fetch_payments(self, since: str, until: str) -> list[dict[str, Any]]:
        """Платежи за период, в виде, готовом для `store.upsert_card_payment`."""
        out = []
        for raw in self._paginate("/payments", {"date_begin": since,
                                                "date_end": until}):
            row = self._payment_row(raw)
            if row:
                out.append(row)
        return out

    @staticmethod
    def _payment_row(p: dict[str, Any]) -> dict[str, Any] | None:
        if not p.get("id"):
            return None
        card = p.get("card") or {}
        user = p.get("user") or {}
        merchant = p.get("merchant") or {}
        state = (p.get("state") or {})
        label = str(state.get("label") or "").strip().lower()
        fees = sum((_dec(f.get("amount")) for f in (p.get("fees") or [])),
                   Decimal("0"))
        return {
            "payment_id": str(p["id"]),
            "card_id": str(card.get("id") or ""),
            "card_last4": str(card.get("last_four") or ""),
            "card_bin": str(card.get("bin") or ""),
            "card_title": str(card.get("title") or ""),
            "user_email": str(user.get("email") or ""),
            "user_name": str(user.get("name") or ""),
            "user_id": str(user.get("id") or ""),
            "team": str((user.get("team") or {}).get("name") or ""),
            "marketer_id": None,
            "merchant": str(merchant.get("name") or ""),
            "descriptor": str(merchant.get("descriptor") or ""),
            "state": STATE_MAP.get(label, label or "unknown"),
            "decline": str(p.get("decline") or ""),
            "is_micro": 1 if p.get("is_micro") else 0,
            "currency": str(p.get("currency") or ""),
            "amount": _dec(p.get("amount")),
            "fee": fees,
            "date": _stamp(p.get("date")),
            "date_final": _stamp(p.get("date_final_state")),
        }

    def fetch_cards(self) -> list[dict[str, Any]]:
        """Карты — чтобы связать платёж с кабинетом и маркетологом."""
        out = []
        for c in self._paginate("/cards", {}):
            if not c.get("id"):
                continue
            user = c.get("user") or {}
            out.append({
                "card_id": str(c["id"]),
                "title": str(c.get("title") or ""),
                "last4": str(c.get("last_four") or ""),
                "bin": str(c.get("bin") or ""),
                "currency": str(c.get("currency") or ""),
                "state": str((c.get("state") or {}).get("label") or ""),
                "archived": bool(c.get("archived")),
                "user_email": str(user.get("email") or ""),
                "user_name": str(user.get("name") or ""),
                "team": str((user.get("team") or {}).get("name") or ""),
            })
        return out


def _stamp(value: Any) -> str:
    """«2026-07-31 13:11:08» → ISO-дата. Пусто — если значения нет."""
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace(" ", "T")


def default_window(days: int = 45) -> tuple[str, str]:
    """Окно перезабора платежей.

    Шире, чем у Meta: финальный статус платежа приходит часами позже, а
    отклонённый может смениться. Забирать один раз и забыть нельзя.
    """
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()
