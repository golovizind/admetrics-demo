#!/usr/bin/env python3
"""
probe.py — разведчик мультитокена FBTOOL.

Задача: по ОДНОМУ мультитокену выяснить фактически, что Meta отдаёт этой личке,
и в каком объёме. Ничего не пишет в Meta и ничего не меняет — только GET.
Результат: JSON-файл со всем сырьём + читаемое резюме в консоль.

Зачем это нужно перед проектированием: набор доступных полей у Graph API
сильно зависит от версии API, типа личности (обычный юзер / бизнес-админ),
уровня доверия аккаунта и того, какой именно app_id зашит в токен FBTOOL.
Гадать бессмысленно — надо померить.

Запуск:
    python3 probe.py --session "eyJ0b2tlbiI6..."           # контейнер FBTOOL (base64 JSON)
    python3 probe.py --session "EAAB..."                    # или голый токен
    python3 probe.py --session-file token.txt --proxy socks5://user:pass@host:port
    python3 probe.py --session ... --max-accounts 5 --out probe_report.json

Ключевая механика: `safe_get` запрашивает широкий список полей; если Meta
ругается «Tried accessing nonexisting field», список делится пополам и
пробуется снова (бинарный поиск). На выходе мы точно знаем, какие поля живые,
а какие эта версия API/этот токен не отдаёт.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

BASE_URL = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"
REQUEST_TIMEOUT = 60
MAX_RETRIES = 4

# Ошибка «нет такого поля» — по ней запускается бинарный поиск живых полей.
NONEXISTING_FIELD_RE = re.compile(
    r"(nonexisting field|Unsupported get request|does not exist|cannot be loaded)", re.I
)


# --------------------------------------------------------------------------- #
# Наборы полей, которые пробуем. Намеренно избыточные — probe сам отсеет лишнее.
# --------------------------------------------------------------------------- #

ME_FIELDS = [
    "id", "name", "first_name", "last_name", "email", "link",
    "locale", "timezone", "age_range", "birthday", "gender", "languages",
]

BUSINESS_FIELDS = [
    "id", "name", "created_time", "created_by", "verification_status",
    "primary_page", "profile_picture_uri", "timezone_id", "two_factor_type",
    "is_disabled_for_integrity_reasons", "vertical", "vertical_id",
    "business_type", "collaborative_ads_managed_partner_business_info",
    "extended_updated_time", "link", "payment_account_id",
]

# Самый важный набор: всё, что описывает рекламный кабинет как финансовый актив.
AD_ACCOUNT_FIELDS = [
    # идентичность
    "id", "account_id", "name", "created_time", "business_name",
    "business", "owner", "partner", "account_status", "disable_reason",
    # деньги
    "currency", "amount_spent", "balance", "spend_cap",
    "min_daily_budget", "min_campaign_group_spend_cap",
    "is_prepay_account", "next_bill_date", "is_direct_deals_enabled",
    "funding_source", "funding_source_details",
    "line_numbers", "media_agency", "io_number",
    # здоровье и лимиты
    "adtrust_dsl", "age", "capabilities", "tos_accepted",
    "user_tasks", "user_access_expire_time", "has_migrated_permissions",
    "is_personal", "is_tax_id_required", "tax_id_status", "tax_id",
    "business_city", "business_country_code", "business_state",
    "business_street", "business_street2", "business_zip",
    # прочее
    "timezone_id", "timezone_name", "timezone_offset_hours_utc",
    "end_advertiser", "end_advertiser_name", "offsite_pixels_tos_accepted",
    "fb_entity", "direct_deals_tos_accepted", "existing_customers",
]

# Транзакции = ФАКТ списания с карты. Именно это сверяется с MyBroCards.
TRANSACTION_FIELDS = [
    "id", "time", "charge_type", "billing_start_time", "billing_end_time",
    "billed_amount_details", "payment_option", "product_type", "status",
    "vat", "vat_invoice_id", "tracking_id", "app_amount", "transaction_type",
    "account_id", "ad_account_ids", "provider_amount",
]

CAMPAIGN_FIELDS = [
    "id", "name", "status", "effective_status", "objective", "buying_type",
    "created_time", "updated_time", "start_time", "stop_time",
    "daily_budget", "lifetime_budget", "budget_remaining", "spend_cap",
    "special_ad_categories", "issues_info", "bid_strategy",
]

AD_FIELDS = [
    "id", "name", "status", "effective_status", "created_time",
    "updated_time", "issues_info", "ad_review_feedback",
    "campaign_id", "adset_id", "preview_shareable_link",
]

PAGE_FIELDS = [
    "id", "name", "category", "verification_status", "fan_count",
    "is_published", "link", "restrictions", "has_transitioned_to_new_page_experience",
]

BUSINESS_USER_FIELDS = ["id", "name", "email", "role", "pending_email", "title"]

PIXEL_FIELDS = ["id", "name", "last_fired_time", "creation_time", "is_created_by_business"]

# Расшифровки кодов Meta — чтобы резюме читалось человеком, а не таблицей чисел.
ACCOUNT_STATUS = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT", 9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE",
    101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}
DISABLE_REASON = {
    0: "NONE", 1: "ADS_INTEGRITY_POLICY", 2: "ADS_IP_REVIEW",
    3: "RISK_PAYMENT", 4: "GRAY_ACCOUNT_SHUT_DOWN", 5: "ADS_AFC_REVIEW",
    6: "BUSINESS_INTEGRITY_RAR", 7: "PERMANENT_CLOSE",
    8: "UNUSED_RESELLER_ACCOUNT", 9: "UNUSED_ACCOUNT",
    10: "UMBRELLA_AD_ACCOUNT", 11: "BUSINESS_MANAGER_INTEGRITY_POLICY",
    12: "MISREPRESENTATION_POLICY", 13: "AFC_FINANCIAL_POLICY",
    14: "SCRAPING_RISK", 15: "COMPROMISED_RISK",
}


class GraphError(RuntimeError):
    def __init__(self, message: str, code: Any = None, subcode: Any = None,
                 http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code,
                "subcode": self.subcode, "http_status": self.http_status}


# --------------------------------------------------------------------------- #
# Разбор контейнера FBTOOL
# --------------------------------------------------------------------------- #
def decode_session_container(value: str) -> dict[str, Any]:
    """Контейнер FBTOOL — это base64 от JSON вида {token, cookies[], ua}."""
    compact = "".join(value.split())
    if not compact:
        raise ValueError("пустой session-контейнер")
    compact += "=" * (-len(compact) % 4)
    try:
        payload = json.loads(base64.b64decode(compact, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("session должен быть Base64-кодированным JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("внутри session ожидается JSON-объект")
    if not isinstance(payload.get("token"), str) or not payload["token"].strip():
        raise ValueError("внутри session отсутствует token")
    return payload


def build_session(raw: str, proxy: str = "") -> tuple[requests.Session, str, dict[str, Any]]:
    """Возвращает (http-сессия, токен, метаданные контейнера)."""
    session = requests.Session()
    raw = raw.strip()
    meta: dict[str, Any] = {"kind": "token", "cookies": 0, "ua": ""}

    if raw.startswith("EAA"):
        token = raw
        session.headers["User-Agent"] = "Internal-Meta-Ads-Analytics/1.1"
    else:
        container = decode_session_container(raw)
        token = str(container["token"]).strip()
        meta["kind"] = "session"
        ua = str(container.get("ua") or "").strip()
        if ua:
            session.headers["User-Agent"] = ua
            meta["ua"] = ua
        for cookie in container.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue
            kwargs: dict[str, Any] = {}
            if cookie.get("domain"):
                kwargs["domain"] = str(cookie["domain"])
            if cookie.get("path"):
                kwargs["path"] = str(cookie["path"])
            session.cookies.set(name, str(cookie.get("value") or ""), **kwargs)
            meta["cookies"] += 1

    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session, token, meta


# --------------------------------------------------------------------------- #
# Клиент Graph API с учётом лимитов
# --------------------------------------------------------------------------- #
class Graph:
    """Тонкая обёртка над Graph API: ретраи, разбор лимитов, бинарный поиск полей."""

    def __init__(self, session: requests.Session, token: str,
                 version: str = DEFAULT_GRAPH_VERSION, verbose: bool = False):
        self.session = session
        self.token = token
        self.version = version
        self.verbose = verbose
        self.calls = 0
        self.usage: dict[str, Any] = {}
        # Кеш «живых» полей по типу сущности. Bisect стоит дорого (log2(N)
        # лишних запросов на каждый нераспознанный кусок), а набор полей для
        # всех кабинетов одинаков — выясняем один раз и переиспользуем.
        self.field_cache: dict[str, tuple[list[str], list[str]]] = {}

    # --- лимиты ---------------------------------------------------------- #
    def _record_usage(self, headers: Any) -> None:
        """Meta присылает бюджет расхода в заголовках. Это единственный
        честный сигнал «пора притормозить» — на проде по нему строится throttling."""
        for header in ("x-app-usage", "x-ad-account-usage",
                       "x-business-use-case-usage"):
            value = headers.get(header)
            if not value:
                continue
            try:
                self.usage[header] = json.loads(value)
            except (TypeError, ValueError):
                self.usage[header] = value

    def worst_usage_pct(self) -> float:
        """Максимальная утилизация лимита по всем счётчикам, в процентах."""
        worst = 0.0

        def scan(node: Any) -> None:
            nonlocal worst
            if isinstance(node, dict):
                for key, val in node.items():
                    if key.endswith("_util_pct") or key in {
                        "call_count", "total_cputime", "total_time",
                        "acc_id_util_pct",
                    }:
                        try:
                            worst = max(worst, float(val))
                        except (TypeError, ValueError):
                            pass
                    else:
                        scan(val)
            elif isinstance(node, list):
                for item in node:
                    scan(item)

        scan(self.usage)
        return worst

    # --- базовый GET ----------------------------------------------------- #
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{BASE_URL}/{self.version}/{str(path).lstrip('/')}"
        query = dict(params or {})
        query["access_token"] = self.token
        last: Exception | None = None

        for attempt in range(MAX_RETRIES):
            self.calls += 1
            try:
                response = self.session.get(url, params=query, timeout=REQUEST_TIMEOUT)
                self._record_usage(response.headers)
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                last = GraphError(f"сеть/разбор ответа: {exc}")
                if attempt == MAX_RETRIES - 1:
                    raise last from exc
                time.sleep(2 ** attempt)
                continue

            if response.ok and "error" not in payload:
                # Мягкий throttling: при >80% утилизации сбрасываем темп.
                if self.worst_usage_pct() > 80:
                    if self.verbose:
                        print("      · лимит >80%, пауза 5 с", file=sys.stderr)
                    time.sleep(5)
                return payload

            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code")
            message = error.get("message", str(payload)[:400])
            transient = bool(error.get("is_transient")) or code in {1, 2, 4, 17, 32, 341, 613}
            last = GraphError(message, code=code,
                              subcode=error.get("error_subcode"),
                              http_status=response.status_code)
            if transient and attempt < MAX_RETRIES - 1:
                retry_after = response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2 ** (attempt + 1))
                continue
            raise last

        raise last or GraphError("не удалось выполнить запрос")

    # --- GET с бинарным поиском живых полей ------------------------------ #
    def get_with_fields(self, path: str, fields: list[str],
                        params: dict[str, Any] | None = None,
                        cache_key: str | None = None
                        ) -> tuple[dict[str, Any], list[str], list[str]]:
        """Пробует запросить `fields`. Если Meta не знает часть полей —
        делит список пополам и рекурсивно ищет рабочее подмножество.

        `cache_key` — тип сущности ('ad_account', 'transaction', …). Если набор
        живых полей для него уже выяснен, bisect не повторяется.

        Возвращает (данные, живые поля, отвергнутые поля).
        """
        if cache_key and cache_key in self.field_cache:
            known_alive, known_dead = self.field_cache[cache_key]
            if not known_alive:
                return {}, [], known_dead
            query = dict(params or {})
            query["fields"] = ",".join(known_alive)
            try:
                return self.get(path, query), list(known_alive), list(known_dead)
            except GraphError:
                # Кабинет мог оказаться урезанным — падаем в обычный bisect.
                pass

        alive: list[str] = []
        dead: list[str] = []
        merged: dict[str, Any] = {}

        def attempt(chunk: list[str]) -> None:
            nonlocal merged
            if not chunk:
                return
            query = dict(params or {})
            query["fields"] = ",".join(chunk)
            try:
                data = self.get(path, query)
            except GraphError as exc:
                if len(chunk) == 1:
                    dead.append(chunk[0])
                    return
                if not NONEXISTING_FIELD_RE.search(str(exc)) and exc.code not in {100, 12}:
                    raise
                mid = len(chunk) // 2
                attempt(chunk[:mid])
                attempt(chunk[mid:])
                return
            alive.extend(chunk)
            if isinstance(data, dict):
                merged.update(data)

        attempt(list(fields))
        if cache_key:
            self.field_cache[cache_key] = (list(alive), list(dead))
        return merged, alive, dead

    # --- пагинация ------------------------------------------------------- #
    def paginate(self, path: str, params: dict[str, Any] | None = None,
                 max_pages: int = 20) -> Iterator[dict[str, Any]]:
        payload = self.get(path, params)
        pages = 0
        while True:
            for item in payload.get("data", []):
                yield item
            pages += 1
            next_url = (payload.get("paging") or {}).get("next")
            if not next_url or pages >= max_pages:
                break
            self.calls += 1
            response = self.session.get(next_url, timeout=REQUEST_TIMEOUT)
            self._record_usage(response.headers)
            payload = response.json()
            if not response.ok or "error" in payload:
                break

    def paginate_fields(self, path: str, fields: list[str],
                        params: dict[str, Any] | None = None,
                        max_pages: int = 20, cache_key: str | None = None
                        ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """Сначала на одной записи выясняем рабочий набор полей, потом
        выкачиваем всю коллекцию уже только живыми полями."""
        probe_params = dict(params or {})
        probe_params["limit"] = 1
        _, alive, dead = self.get_with_fields(path, fields, probe_params,
                                              cache_key=cache_key)
        if not alive:
            return [], [], dead
        full_params = dict(params or {})
        full_params["fields"] = ",".join(alive)
        full_params.setdefault("limit", 100)
        return list(self.paginate(path, full_params, max_pages)), alive, dead


# --------------------------------------------------------------------------- #
# Шаги разведки
# --------------------------------------------------------------------------- #
def safe(label: str, fn: Any, report: dict[str, Any], verbose: bool = True) -> Any:
    """Выполняет шаг, ловит ошибку и кладёт её в отчёт вместо падения."""
    if verbose:
        print(f"  → {label} ...", end="", flush=True)
    try:
        result = fn()
    except GraphError as exc:
        report.setdefault("errors", {})[label] = exc.as_dict()
        if verbose:
            print(f" ОШИБКА: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — разведка не должна падать целиком
        report.setdefault("errors", {})[label] = {"error": repr(exc)}
        if verbose:
            print(f" ОШИБКА: {exc!r}")
        return None
    if verbose:
        size = len(result) if isinstance(result, (list, dict)) else 1
        print(f" ok ({size})")
    return result


def minor_to_major(value: Any) -> float:
    """amount_spent / balance / spend_cap Meta отдаёт в МИНОРНЫХ единицах
    (центы). Забыть об этом — классическая ошибка на порядок в отчётности."""
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def probe(raw_session: str, proxy: str = "", version: str = DEFAULT_GRAPH_VERSION,
          max_accounts: int = 10, days: int = 30, deep: bool = False,
          verbose: bool = True) -> dict[str, Any]:
    session, token, container_meta = build_session(raw_session, proxy)
    api = Graph(session, token, version, verbose=verbose)

    report: dict[str, Any] = {
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "graph_version": version,
        "container": container_meta,
        "field_support": {},
        "errors": {},
    }

    # 1. Кто мы -------------------------------------------------------------
    print("\n[1/7] Личность токена")
    me, alive, dead = api.get_with_fields("me", ME_FIELDS)
    report["me"] = me
    report["field_support"]["me"] = {"alive": alive, "dead": dead}
    print(f"  → {me.get('name', '?')} (id {me.get('id', '?')}) · "
          f"полей живых {len(alive)}, недоступных {len(dead)}")

    # Права токена — сразу видно, что вообще можно спрашивать.
    perms = safe("permissions", lambda: list(api.paginate("me/permissions")), report)
    report["permissions"] = perms or []
    granted = sorted(p["permission"] for p in (perms or [])
                     if p.get("status") == "granted")
    report["permissions_granted"] = granted
    if granted:
        print(f"      права: {', '.join(granted[:12])}"
              f"{' …' if len(granted) > 12 else ''}")

    # Кто выдал токен: app_id критичен — от него зависит потолок доступа.
    app = safe("token_app", lambda: api.get("app", {"fields": "id,name,category"}), report)
    report["app"] = app or {}

    # 2. Бизнес-менеджеры ---------------------------------------------------
    print("\n[2/7] Бизнес-менеджеры")
    businesses, b_alive, b_dead = api.paginate_fields("me/businesses", BUSINESS_FIELDS, cache_key="business")
    report["businesses"] = businesses
    report["field_support"]["business"] = {"alive": b_alive, "dead": b_dead}
    print(f"  → найдено BM: {len(businesses)}")

    for biz in businesses:
        bid = biz.get("id")
        if not bid:
            continue
        biz["_users"] = safe(
            f"BM {bid} users",
            lambda b=bid: api.paginate_fields(f"{b}/business_users",
                                              BUSINESS_USER_FIELDS,
                                              cache_key="business_user")[0],
            report, verbose=False) or []
        biz["_pending_users"] = safe(
            f"BM {bid} pending_users",
            lambda b=bid: list(api.paginate(f"{b}/pending_users",
                                            {"fields": "id,email,role"})),
            report, verbose=False) or []
        # Кредитные линии агентского BM — прямая финансовая сущность.
        biz["_extendedcredits"] = safe(
            f"BM {bid} extendedcredits",
            lambda b=bid: list(api.paginate(
                f"{b}/extendedcredits",
                {"fields": "id,legal_entity_name,max_balance,balance,"
                           "credit_available,credit_type,is_access_revoked"})),
            report, verbose=False) or []
        biz["_pages"] = safe(
            f"BM {bid} owned_pages",
            lambda b=bid: api.paginate_fields(f"{b}/owned_pages", PAGE_FIELDS,
                                              max_pages=3, cache_key="page")[0],
            report, verbose=False) or []
        print(f"      · {biz.get('name', bid)}: юзеров {len(biz['_users'])}, "
              f"страниц {len(biz['_pages'])}, "
              f"кредитных линий {len(biz['_extendedcredits'])}, "
              f"verification={biz.get('verification_status', '—')}")

    # 3. Рекламные кабинеты — главный актив ---------------------------------
    print("\n[3/7] Рекламные кабинеты")
    accounts, a_alive, a_dead = api.paginate_fields("me/adaccounts", AD_ACCOUNT_FIELDS,
                                                   cache_key="ad_account")
    report["field_support"]["ad_account"] = {"alive": a_alive, "dead": a_dead}
    print(f"  → через me/adaccounts: {len(accounts)}")
    print(f"      живых полей {len(a_alive)}; недоступны: "
          f"{', '.join(a_dead) if a_dead else '—'}")

    # Кабинеты, привязанные к BM, часто НЕ видны в me/adaccounts. Добираем.
    seen = {a.get("id") for a in accounts}
    for biz in businesses:
        bid = biz.get("id")
        for edge in ("owned_ad_accounts", "client_ad_accounts"):
            extra = safe(
                f"BM {bid} {edge}",
                lambda b=bid, e=edge: api.paginate_fields(
                    f"{b}/{e}", AD_ACCOUNT_FIELDS, cache_key="ad_account")[0],
                report, verbose=False) or []
            added = 0
            for account in extra:
                if account.get("id") not in seen:
                    account["_source"] = f"{edge}@{bid}"
                    accounts.append(account)
                    seen.add(account.get("id"))
                    added += 1
            if added:
                print(f"      + {added} из {edge} BM {biz.get('name', bid)}")

    report["ad_accounts"] = accounts
    print(f"  → ИТОГО уникальных кабинетов: {len(accounts)}")

    # 4. Финансовый срез по кабинетам ---------------------------------------
    print(f"\n[4/7] Финансы по кабинетам (первые {max_accounts})")
    finance_rows: list[dict[str, Any]] = []
    for account in accounts[:max_accounts]:
        acct_id = account.get("id")
        if not acct_id:
            continue
        status = account.get("account_status")
        row = {
            "id": acct_id,
            "name": account.get("name", ""),
            "currency": account.get("currency", ""),
            "status": ACCOUNT_STATUS.get(status, status),
            "disable_reason": DISABLE_REASON.get(account.get("disable_reason"),
                                                 account.get("disable_reason")),
            "amount_spent": minor_to_major(account.get("amount_spent")),
            "balance": minor_to_major(account.get("balance")),
            "spend_cap": minor_to_major(account.get("spend_cap")),
            # adtrust_dsl — дневной лимит открута. Главный сигнал «здоровья».
            "daily_spend_limit": minor_to_major(account.get("adtrust_dsl")),
            "funding": (account.get("funding_source_details") or {}).get("display_string", ""),
            "funding_type": (account.get("funding_source_details") or {}).get("type", ""),
            "business": (account.get("business") or {}).get("name", ""),
        }

        # Транзакции — это фактические списания с карты, а не оценка спенда.
        tx, tx_alive, tx_dead = (safe(
            f"{acct_id} transactions",
            lambda a=acct_id: api.paginate_fields(
                f"{a}/transactions", TRANSACTION_FIELDS,
                {"time_start": int(time.time()) - days * 86400},
                max_pages=5, cache_key="transaction"),
            report, verbose=False) or ([], [], TRANSACTION_FIELDS))
        row["transactions_count"] = len(tx)
        row["transactions_sample"] = tx[:3]
        report["field_support"].setdefault("transaction", {"alive": tx_alive, "dead": tx_dead})

        finance_rows.append(row)
        flag = "" if row["status"] == "ACTIVE" else f"  ⚠ {row['status']}"
        print(f"      · {row['name'][:34]:34} {row['currency']:4} "
              f"spent={row['amount_spent']:>12,.2f} "
              f"dsl={row['daily_spend_limit']:>10,.2f} "
              f"tx={row['transactions_count']:>4} "
              f"card={row['funding'] or '—'}{flag}")

    report["finance"] = finance_rows

    # 5. Инсайты — проверяем реальную гранулярность -------------------------
    print("\n[5/7] Инсайты (гранулярность и объём)")
    insight_probe: dict[str, Any] = {}
    if accounts:
        acct_id = accounts[0]["id"]
        for level in ("account", "campaign", "adset", "ad"):
            rows = safe(
                f"insights level={level}",
                lambda a=acct_id, lv=level: list(api.paginate(
                    f"{a}/insights",
                    {"level": lv, "time_increment": 1,
                     "date_preset": "last_30d", "limit": 100,
                     "fields": "account_id,campaign_id,adset_id,ad_id,spend,"
                               "impressions,clicks,actions,date_start"},
                    max_pages=2)),
                report, verbose=False) or []
            insight_probe[level] = len(rows)
            print(f"      · level={level:9} строк за 30 дней: {len(rows)}")
    report["insights_probe"] = insight_probe

    # 6. Здоровье: кампании и объявления с issues_info -----------------------
    print("\n[6/7] Здоровье кампаний/объявлений")
    health: dict[str, Any] = {}
    for account in accounts[:(max_accounts if deep else 3)]:
        acct_id = account.get("id")
        campaigns = safe(f"{acct_id} campaigns",
                         lambda a=acct_id: api.paginate_fields(
                             f"{a}/campaigns", CAMPAIGN_FIELDS, max_pages=3,
                             cache_key="campaign")[0],
                         report, verbose=False) or []
        ads = safe(f"{acct_id} ads",
                   lambda a=acct_id: api.paginate_fields(
                       f"{a}/ads", AD_FIELDS, max_pages=3, cache_key="ad")[0],
                   report, verbose=False) or []
        with_issues = [a for a in ads if a.get("issues_info")]
        health[acct_id] = {
            "campaigns": len(campaigns),
            "ads": len(ads),
            "ads_with_issues": len(with_issues),
            "issues_sample": with_issues[:3],
        }
        print(f"      · {account.get('name', acct_id)[:34]:34} "
              f"кампаний {len(campaigns):>4}, объявлений {len(ads):>4}, "
              f"с проблемами {len(with_issues):>3}")
    report["health"] = health

    # 7. Прочие сущности -----------------------------------------------------
    print("\n[7/7] Прочее")
    report["pages"] = safe("me/accounts (страницы)",
                           lambda: api.paginate_fields("me/accounts", PAGE_FIELDS, cache_key="page")[0],
                           report) or []
    if accounts:
        report["pixels"] = safe(
            "adspixels",
            lambda: api.paginate_fields(f"{accounts[0]['id']}/adspixels",
                                        PIXEL_FIELDS, cache_key="pixel")[0],
            report) or []

    # 8. Комментарии — отдельная цепочка прав ------------------------------
    print("\n[8/8] Комментарии под объявлениями")
    report["comments"] = probe_comments(api, accounts, report)

    report["api_calls"] = api.calls
    report["rate_limit_usage"] = api.usage
    report["rate_limit_worst_pct"] = api.worst_usage_pct()
    return report


def probe_comments(api: "Graph", accounts: list[dict[str, Any]],
                   report: dict[str, Any]) -> dict[str, Any]:
    """Можно ли этим токеном читать и модерировать комментарии.

    Цепочка, по которой работает FBTOOL, состоит из трёх звеньев, и рвётся
    она обычно на первом:

      1. `/me/accounts` должен вернуть поле `access_token` — это токен
         СТРАНИЦЫ. Комментарии живут на странице, пользовательским токеном
         их не модерировать. Поле отдаётся только при правах
         `pages_show_list` + `pages_read_engagement`.
      2. объявление → `creative{effective_object_story_id}` = `{page}_{post}`.
         Для дарк-постов это единственный способ узнать, под каким постом
         лежат комментарии.
      3. `GET /{post}/comments` — чтение; скрытие и удаление требуют
         `pages_manage_engagement`.

    Тут только ЧИТАЕМ: ни одного комментария не скрываем и не удаляем.
    Право на запись выводится из наличия permission, а не проверкой боем.
    """
    out: dict[str, Any] = {"page_tokens": 0, "pages": [], "post_ids": 0,
                           "comments_readable": None, "can_moderate": None}

    granted = set(report.get("permissions_granted", []))
    out["has_read_permission"] = "pages_read_engagement" in granted
    out["has_manage_permission"] = "pages_manage_engagement" in granted

    pages = safe("страницы с page-токеном",
                 lambda: list(api.paginate(
                     "me/accounts", {"fields": "id,name,access_token,tasks"})),
                 report, verbose=False) or []
    for page in pages:
        has_token = bool(page.get("access_token"))
        if has_token:
            out["page_tokens"] += 1
        out["pages"].append({"id": page.get("id"), "name": page.get("name"),
                             "has_token": has_token,
                             "tasks": page.get("tasks")})
    print(f"      · страниц: {len(pages)}, из них с page-токеном: "
          f"{out['page_tokens']}")

    if not out["page_tokens"]:
        out["verdict"] = ("страницы не отдают access_token — модерация "
                          "комментариев этим токеном невозможна")
        print(f"      · ВЫВОД: {out['verdict']}")
        return out

    # Ищем реальный пост объявления, чтобы проверить чтение комментариев.
    story_id = None
    for account in accounts[:3]:
        creatives = safe(
            f"{account.get('id')} adcreatives",
            lambda a=account: list(api.paginate(
                f"{a['id']}/adcreatives",
                {"fields": "id,effective_object_story_id,object_story_id",
                 "limit": 25}, max_pages=1)),
            report, verbose=False) or []
        for creative in creatives:
            story_id = (creative.get("effective_object_story_id")
                        or creative.get("object_story_id"))
            if story_id:
                break
        if story_id:
            break

    out["post_ids"] = 1 if story_id else 0
    if not story_id:
        out["verdict"] = ("page-токены есть, но пост объявления не найден — "
                          "проверьте на кабинете с активными креативами")
        print(f"      · ВЫВОД: {out['verdict']}")
        return out

    # story_id = "{page_id}_{post_id}". Берём страницу по префиксу, а не
    # по split("_")[0]: у Meta id числовые, но полагаться на отсутствие
    # подчёркиваний внутри идентификатора не стоит.
    story = str(story_id)
    page_id, page_token = "", None
    for candidate in pages:
        cid = str(candidate.get("id") or "")
        if cid and story.startswith(cid + "_") and candidate.get("access_token"):
            page_id, page_token = cid, candidate["access_token"]
            break
    if not page_id:
        page_id = story.split("_")[0]
    if not page_token:
        out["verdict"] = (f"пост найден ({story_id}), но токена его страницы "
                          f"{page_id} нет — эта страница не под нашим доступом")
        print(f"      · ВЫВОД: {out['verdict']}")
        return out

    # Читаем комментарии токеном СТРАНИЦЫ, а не пользователя.
    page_api = Graph(api.session, page_token, api.version)
    comments = safe(
        f"комментарии поста {story_id}",
        lambda: list(page_api.paginate(
            f"{story_id}/comments",
            {"fields": "id,message,from,created_time,is_hidden", "limit": 5},
            max_pages=1)),
        report, verbose=False)
    out["comments_readable"] = comments is not None
    out["sample_count"] = len(comments or [])
    out["can_moderate"] = bool(out["has_manage_permission"]
                               and out["comments_readable"])
    out["verdict"] = (
        "чтение и модерация доступны" if out["can_moderate"] else
        "комментарии читаются, но нет pages_manage_engagement — "
        "скрывать и удалять нельзя" if out["comments_readable"] else
        "page-токен есть, но комментарии не читаются")
    print(f"      · комментариев в выборке: {out['sample_count']}")
    print(f"      · ВЫВОД: {out['verdict']}")
    return out


# --------------------------------------------------------------------------- #
# Резюме
# --------------------------------------------------------------------------- #
def summarize(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append

    me = report.get("me", {})
    accounts = report.get("ad_accounts", [])
    finance = report.get("finance", [])

    add("=" * 78)
    add("РЕЗЮМЕ РАЗВЕДКИ")
    add("=" * 78)
    add(f"Личка          : {me.get('name', '?')} (id {me.get('id', '?')})")
    add(f"Контейнер      : {report['container']['kind']}, "
        f"cookies {report['container']['cookies']}")
    add(f"App токена     : {report.get('app', {}).get('name', '—')} "
        f"(id {report.get('app', {}).get('id', '—')})")
    add(f"Права          : {len(report.get('permissions_granted', []))} шт.")
    add(f"BM             : {len(report.get('businesses', []))}")
    add(f"Кабинеты       : {len(accounts)}")
    add(f"Запросов к API : {report.get('api_calls', 0)}  "
        f"(утилизация лимита {report.get('rate_limit_worst_pct', 0):.0f}%)")
    add("")

    # Деньги
    by_currency: dict[str, float] = {}
    for row in finance:
        by_currency[row["currency"]] = by_currency.get(row["currency"], 0.0) + row["amount_spent"]
    if by_currency:
        add("ПОТРАЧЕНО ВСЕГО (amount_spent, по валютам):")
        for cur, total in sorted(by_currency.items(), key=lambda kv: -kv[1]):
            add(f"  {cur or '???'}: {total:,.2f}")
        add("")

    # Здоровье
    statuses: dict[str, int] = {}
    for row in finance:
        statuses[str(row["status"])] = statuses.get(str(row["status"]), 0) + 1
    if statuses:
        add("СТАТУСЫ КАБИНЕТОВ:")
        for status, count in sorted(statuses.items(), key=lambda kv: -kv[1]):
            add(f"  {status}: {count}")
        add("")

    # Карты — мостик к MyBroCards
    cards = sorted({r["funding"] for r in finance if r["funding"]})
    add(f"ПРИВЯЗАННЫЕ ИСТОЧНИКИ ОПЛАТЫ ({len(cards)}): "
        f"{', '.join(cards) if cards else '— не отдаётся'}")
    add("  ↑ это ключ для сверки с MyBroCards (последние 4 цифры карты)")
    add("")

    # Что доступно, а что нет — главный ответ разведки
    add("ДОСТУПНОСТЬ ПОЛЕЙ (что API реально отдал этому токену):")
    for entity, support in report.get("field_support", {}).items():
        dead = support.get("dead", [])
        add(f"  {entity:14} живых {len(support.get('alive', [])):>3}, "
            f"недоступных {len(dead):>3}"
            f"{': ' + ', '.join(dead) if dead else ''}")
    add("")

    ins = report.get("insights_probe", {})
    if ins:
        add("ГРАНУЛЯРНОСТЬ ИНСАЙТОВ (строк за 30 дней, 1 кабинет):")
        for level, count in ins.items():
            add(f"  level={level:9} {count}")
        add("")

    comments = report.get("comments") or {}
    if comments:
        add("КОММЕНТАРИИ ПОД ОБЪЯВЛЕНИЯМИ:")
        add(f"  страниц с page-токеном : {comments.get('page_tokens', 0)}")
        add(f"  pages_read_engagement  : "
            f"{'да' if comments.get('has_read_permission') else 'НЕТ'}")
        add(f"  pages_manage_engagement: "
            f"{'да' if comments.get('has_manage_permission') else 'НЕТ'}")
        add(f"  вывод                  : {comments.get('verdict', '—')}")
        add("")

    errors = report.get("errors", {})
    if errors:
        add(f"ОШИБКИ ({len(errors)}) — что этому токену закрыто:")
        for label, err in list(errors.items())[:20]:
            add(f"  · {label}: {str(err.get('error'))[:110]}")
        if len(errors) > 20:
            add(f"  … и ещё {len(errors) - 20}")
    add("=" * 78)
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Разведка мультитокена FBTOOL: что реально отдаёт Meta")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--session", help="контейнер FBTOOL (base64) или токен EAA…")
    src.add_argument("--session-file", help="файл с контейнером/токеном")
    p.add_argument("--proxy", default=os.environ.get("PROBE_PROXY", ""),
                   help="socks5://user:pass@host:port или http://…")
    p.add_argument("--graph-version", default=DEFAULT_GRAPH_VERSION)
    p.add_argument("--max-accounts", type=int, default=10,
                   help="сколько кабинетов обходить детально (по деньгам)")
    p.add_argument("--days", type=int, default=30,
                   help="глубина выборки транзакций, дней")
    p.add_argument("--deep", action="store_true",
                   help="глубокая проверка здоровья по всем кабинетам из --max-accounts")
    p.add_argument("--out", default="probe_report.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    raw = (open(args.session_file, encoding="utf-8").read()
           if args.session_file else args.session)

    report = probe(raw, proxy=args.proxy, version=args.graph_version,
                   max_accounts=args.max_accounts, days=args.days, deep=args.deep)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)

    print("\n" + summarize(report))
    print(f"\nСырой отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
