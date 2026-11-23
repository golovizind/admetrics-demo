"""Клиент SMM-панели smmlaba (стандарт Perfect Panel API v2).

Все панели этого движка говорят одинаково: POST на /api/v2 формой, где `key`
— ключ, `action` — метод. Ключ живёт в окружении (SMMLABA_API_KEY) и наружу,
во фронт, не отдаётся — панель дёргает только сервер.

Деньги: `add` тратит баланс панели. Поэтому заказ делает только явное действие
пользователя в интерфейсе, а не фоновые задачи.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

def _ca_bundle() -> Any:
    """Валидный путь к CA-бандлу или True. На сервере certifi в venv бывает
    битым (нет cacert.pem) или на него указывает пустая переменная окружения —
    тогда requests падает ещё до запроса. Явный verify перебивает оба случая:
    берём certifi, если файл реально есть, иначе системный бандл Linux."""
    try:
        import certifi
        path = certifi.where()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    for path in ("/etc/ssl/certs/ca-certificates.crt",
                 "/etc/pki/tls/certs/ca-bundle.crt",
                 "/etc/ssl/cert.pem"):
        if os.path.exists(path):
            return path
    return True


BASE_URL = os.environ.get("SMMLABA_BASE_URL", "https://smmlaba.com/vkapi/v1/")
TIMEOUT = 30
MAX_RETRIES = 3


class SmmError(Exception):
    pass


def _redact(text: str, key: str) -> str:
    """Убрать ключ из любого текста, прежде чем он попадёт в лог или ответ."""
    return text.replace(key, "***") if key else text


class Smm:
    def __init__(self, key: str = "", base_url: str = ""):
        self.key = (key or os.environ.get("SMMLABA_API_KEY", "")).strip()
        # слэш на конце сохраняем: у smmlaba адрес /vkapi/v1/ именно со
        # слэшем, иначе лишний 307-редирект на канонический путь.
        self.base = (base_url or BASE_URL).rstrip("/") + "/"
        self.calls = 0
        if not self.key:
            raise SmmError("Не задан SMMLABA_API_KEY")
        self.session = requests.Session()
        self.session.verify = _ca_bundle()
        # Представляемся явным именем клиента: дефолтный UA python-requests
        # некоторые хостинги режут фильтром, а это обычный интеграционный
        # клиент к своему же оплаченному API-кабинету.
        self.session.headers.update({
            "User-Agent": "Admetrics/1.0 (+https://admetrics.example.com)",
            "Accept": "application/json, text/plain, */*",
        })

    def _post(self, data: dict[str, Any]) -> Any:
        payload = {"key": self.key, **{k: v for k, v in data.items()
                                       if v is not None}}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self.calls += 1
            try:
                r = self.session.post(self.base, data=payload, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last = SmmError(_redact(f"сеть: {exc}", self.key))
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if not r.ok:
                raise SmmError(_redact(f"{r.status_code}: {r.text[:200]}",
                                       self.key))
            try:
                body = r.json()
            except ValueError as exc:
                raise SmmError(_redact(f"не JSON: {r.text[:200]}",
                                       self.key)) from exc
            # Панель отдаёт ошибку полем `error` при HTTP 200.
            if isinstance(body, dict) and body.get("error"):
                raise SmmError(_redact(str(body["error"]), self.key))
            return body
        raise last or SmmError("не удалось получить ответ панели")

    # ------------------------------------------------------------------ #
    def balance(self) -> dict[str, Any]:
        """{balance, currency}."""
        return self._post({"action": "balance"})

    def services(self) -> list[dict[str, Any]]:
        """Каталог услуг: [{service, name, type, category, rate, min, max,
        dripfeed, refill, cancel}]."""
        got = self._post({"action": "services"})
        return got if isinstance(got, list) else []

    def add(self, service: Any, link: str, quantity: Any = None,
            **extra: Any) -> dict[str, Any]:
        """Создать заказ. Возвращает {order: id}. extra — runs/interval/
        comments/… для типов услуг, которым они нужны."""
        data = {"action": "add", "service": service, "link": link,
                "quantity": quantity, **extra}
        return self._post(data)

    def status(self, order: Any) -> dict[str, Any]:
        """{charge, start_count, status, remains, currency} одного заказа."""
        return self._post({"action": "status", "order": order})

    def status_many(self, orders: list[Any]) -> dict[str, Any]:
        """Статусы пачкой: {order_id: {charge, status, remains, ...}}.
        Панель ограничивает ~100 id за раз."""
        if not orders:
            return {}
        return self._post({"action": "status",
                           "orders": ",".join(str(o) for o in orders)})

    def refill(self, order: Any) -> dict[str, Any]:
        return self._post({"action": "refill", "order": order})

    def cancel(self, orders: list[Any]) -> Any:
        return self._post({"action": "cancel",
                          "orders": ",".join(str(o) for o in orders)})
