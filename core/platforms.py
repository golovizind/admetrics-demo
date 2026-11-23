"""
Абстракция рекламных платформ.

Контракт взят из `platforms.py` SpendTool — это самая переиспользуемая часть
их заготовки, и ломать её незачем. Расширен методами, которых там не было и
без которых не решается задача бизнеса: реестр активов и биллинг.

    discover_accounts()                 — кабинеты личики как АКТИВЫ
    fetch_spend(acc, since, until)      — итог по кабинету за день (авторитетный)
    fetch_spend_countries(...)          — разбивка по фактической доставке
    fetch_spend_campaigns(...)          — по кампаниям
    fetch_transactions(acc, since)      — фактические списания (новое)
    fetch_businesses()                  — BM личики (новое)
    whoami()                            — health-check личики

По решению проекта реализация пока одна — Facebook. Абстракция сохраняется,
чтобы добавление TikTok позже стоило дёшево: адаптер SpendTool готов примерно
на 60% и дописывается в этот же интерфейс.

Все денежные величины возвращаются в МАЖОРНЫХ единицах (Decimal), потому что
Meta отдаёт часть полей в центах, и оставлять эту разницу вызывающему коду —
верный способ получить ошибку на два порядка в отчётности.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .graph import (ACCOUNT_STATUS, ACCOUNT_STATUS_RU, DEFAULT_GRAPH_VERSION,
                    DISABLE_REASON, DISABLE_REASON_RU, AuthError, Graph,
                    GraphError, TransientError, build_session)

ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    if value is None:
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return ZERO


def _minor(value: Any) -> Decimal:
    """Центы → основная единица валюты."""
    return _dec(value) / Decimal(100)


# --------------------------------------------------------------------------- #
# Наборы полей
# --------------------------------------------------------------------------- #
ACCOUNT_FIELDS = [
    "id", "account_id", "name", "created_time", "business", "business_name",
    "account_status", "disable_reason", "currency", "timezone_name",
    "amount_spent", "balance", "spend_cap", "adtrust_dsl",
    "funding_source_details", "min_daily_budget", "next_bill_date",
    "is_prepay_account", "owner",
]
BUSINESS_FIELDS = [
    "id", "name", "created_time", "verification_status", "primary_page",
    "is_disabled_for_integrity_reasons",
]
TRANSACTION_FIELDS = [
    "id", "time", "charge_type", "billing_start_time", "billing_end_time",
    "amount", "payment_option", "product_type", "status", "vat",
]

INSIGHT_BASE = ["spend", "impressions", "clicks", "actions", "action_values",
                "reach", "inline_link_clicks", "date_start", "account_id"]

# Разбивки открута. Проверены на живом токене: всё, кроме `platform_position`,
# который Meta отказывается считать вместе с `actions`.
BREAKDOWNS = {
    "age": "age",
    "gender": "gender",
    "platform": "publisher_platform",
    "device": "impression_device",
    "region": "region",
}

# Поля креатива — то, из чего складывается «что именно крутится».
# `preview_shareable_link` живёт на самом объявлении и даёт короткую ссылку
# вида fb.me/… — по ней креатив открывается без доступа к кабинету.
CREATIVE_FIELDS = ["id", "title", "body", "thumbnail_url", "image_url",
                   "object_type", "call_to_action_type", "link_url",
                   "effective_object_story_id", "instagram_permalink_url",
                   "object_story_spec"]

# Кампания и объявление как СОСТОЯНИЕ. `issues_info` — это реджекты с текстом
# причины: то, что маркетолог иначе идёт искать руками в Ads Manager.
CAMPAIGN_FIELDS = [
    "id", "name", "status", "effective_status", "objective", "buying_type",
    "created_time", "updated_time", "daily_budget", "lifetime_budget",
    "budget_remaining", "bid_strategy", "issues_info",
]
AD_FIELDS = [
    "id", "name", "status", "effective_status", "campaign_id", "adset_id",
    "issues_info", "updated_time",
]

# Типы действий, которые считаем лидами и покупками. Meta называет одно и то же
# по-разному в зависимости от того, как настроена цель и пиксель.
LEAD_KEYS = ["lead", "onsite_conversion.lead_grouped",
             "offsite_conversion.fb_pixel_lead", "onsite_web_lead"]
PURCHASE_KEYS = ["purchase", "omni_purchase",
                 "offsite_conversion.fb_pixel_purchase"]



def _names(node: Any) -> str:
    """Имена из структур таргетинга. Meta кладёт их то списком объектов,
    то списком списков (`flexible_spec`), то словарём по типам — поэтому
    обход общий, а не по одной известной форме."""
    out: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if x.get("name"):
                out.append(str(x["name"]))
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(node)
    # Порядок сохраняем, повторы убираем: одни и те же интересы приходят
    # в нескольких блоках flexible_spec.
    seen: dict[str, None] = {}
    for name in out:
        seen.setdefault(name, None)
    return ", ".join(seen)[:500]


# Автоправила Meta описывает машинным языком: `spend GREATER_THAN 100`.
# Хранить это как есть — значит каждый раз переводить его глазами, поэтому
# перевод делается один раз здесь, а сырой spec всё равно остаётся в JSON.
_RULE_OPS = {
    "GREATER_THAN": ">", "LESS_THAN": "<", "EQUAL": "=", "NOT_EQUAL": "≠",
    "IN_RANGE": "в диапазоне", "NOT_IN_RANGE": "вне диапазона",
    "IN": "среди", "NOT_IN": "не среди", "CONTAIN": "содержит",
    "NOT_CONTAIN": "не содержит", "ANY": "любое", "ALL": "все",
    "NONE": "ничего",
}
# Поля условий. Переводим только те, в которых уверены, — остальные
# остаются как есть: неверный перевод хуже английского названия, потому
# что английское маркетолог хотя бы найдёт в Ads Manager поиском.
_RULE_FIELDS = {
    "spend": "расход", "impressions": "показы", "clicks": "клики",
    "reach": "охват", "frequency": "частота", "results": "результаты",
    "cost_per_result": "цена результата", "lifetime_budget": "бюджет на весь срок",
    "daily_budget": "дневной бюджет", "age": "возраст объявления",
}
_RULE_SCHEDULES = {
    "SEMI_HOURLY": "каждые полчаса", "DAILY": "раз в сутки",
    "HOURLY": "раз в час", "CUSTOM": "по расписанию",
}


def _rule_condition(spec: Any) -> str:
    """Условие правила одной строкой: «spend > 100, roas < 1»."""
    parts: list[str] = []
    for f in ((spec or {}).get("filters") or []):
        field = str(f.get("field") or "")
        if not field or field in ("entity_type", "time_preset"):
            continue  # служебные фильтры, а не условие срабатывания
        op = _RULE_OPS.get(str(f.get("operator") or ""), str(f.get("operator") or ""))
        field = _RULE_FIELDS.get(field, field)
        value = f.get("value")
        if isinstance(value, (list, tuple)):
            # Диапазон читается как «2–5», перечисление — через запятую.
            # Одна и та же запятая в обоих случаях превращала «от 2 до 5»
            # в «2 или 5», а это разные правила.
            joiner = "–" if "RANGE" in str(f.get("operator") or "") else ", "
            value = joiner.join(str(v) for v in value)
        parts.append(" ".join(x for x in (field, op, str(value or "")) if x))
    return "; ".join(parts)[:400]


def _rule_schedule(spec: Any) -> str:
    """Как часто правило проверяется."""
    kind = str((spec or {}).get("schedule_type") or "")
    return _RULE_SCHEDULES.get(kind, kind)


def _iso_from_epoch(value: Any) -> str:
    """Unix-секунды → ISO-строка. Пусто, если это уже строка или мусор."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _by_action_type(items: Any) -> dict[str, str]:
    return {str(i.get("action_type")): str(i.get("value", ""))
            for i in (items or []) if i.get("action_type")}


def _pick(mapping: dict[str, str], keys: list[str]) -> Decimal:
    for key in keys:
        if key in mapping:
            return _dec(mapping[key])
    return ZERO


class PlatformAdapter:
    """Интерфейс. Каждая платформа реализует его целиком."""

    name = "base"

    def whoami(self) -> dict[str, Any]:
        raise NotImplementedError

    def discover_accounts(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_businesses(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_spend(self, act_id: str, since: str, until: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_spend_countries(self, act_id: str, since: str,
                              until: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_spend_campaigns(self, act_id: str, since: str,
                              until: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_spend_ads(self, act_id: str, since: str,
                        until: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_breakdown(self, act_id: str, kind: str, since: str,
                        until: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_transactions(self, act_id: str, since_days: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_campaign_entities(self, act_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_ad_entities(self, act_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError


class FacebookAdapter(PlatformAdapter):
    name = "fb"

    def __init__(self, secret: str, proxy: str = "", version: str | None = None):
        session, token, meta = build_session(secret, proxy)
        self.api = Graph(session, token, version or DEFAULT_GRAPH_VERSION)
        self.container = meta

    # ---------------------------------------------------------------- #
    def whoami(self) -> dict[str, Any]:
        """Health-check личики. Бросает AuthError, если токен мёртв —
        по этому исключению коллектор ставит `needs_reconnect` и зовёт
        маркетолога, вместо того чтобы молча собрать ноль."""
        data, _, _ = self.api.get_with_fields("me", ["id", "name"])
        return data

    def fetch_businesses(self) -> list[dict[str, Any]]:
        found, _, _ = self.api.paginate_fields("me/businesses", BUSINESS_FIELDS,
                                               cache_key="business")
        return [{
            "bm_id": b.get("id"),
            "name": b.get("name", ""),
            "verification_status": b.get("verification_status", ""),
            "is_disabled_for_integrity": int(bool(
                b.get("is_disabled_for_integrity_reasons"))),
            "created_time": b.get("created_time"),
        } for b in found if b.get("id")]

    def discover_accounts(self) -> list[dict[str, Any]]:
        """Кабинеты личики как активы.

        Собираются из трёх источников и склеиваются по id: `me/adaccounts`
        плюс рёбра каждого BM. Агентские и шаренные кабинеты в `me/adaccounts`
        часто не появляются — брать только его значит терять активы, которые
        бизнес как раз и просит посчитать.
        """
        found, alive, _ = self.api.paginate_fields("me/adaccounts", ACCOUNT_FIELDS,
                                                   cache_key="ad_account")
        seen = {a.get("id") for a in found}

        for biz in self.fetch_businesses():
            for edge in ("owned_ad_accounts", "client_ad_accounts"):
                try:
                    extra, _, _ = self.api.paginate_fields(
                        f"{biz['bm_id']}/{edge}", alive or ACCOUNT_FIELDS,
                        cache_key="ad_account")
                except GraphError:
                    continue  # часть BM закрыта для этого токена — это норма
                for account in extra:
                    if account.get("id") and account["id"] not in seen:
                        seen.add(account["id"])
                        found.append(account)

        return [self._account_row(a) for a in found if a.get("id")]

    @staticmethod
    def _account_row(a: dict[str, Any]) -> dict[str, Any]:
        funding = a.get("funding_source_details") or {}
        display = str(funding.get("display_string") or "")
        # Из «Visa *4242» нужны последние 4 цифры — это ключ сверки с MyBroCards.
        digits = "".join(ch for ch in display if ch.isdigit())
        status = a.get("account_status")
        reason = a.get("disable_reason")
        return {
            "act_id": a["id"],
            "platform": "fb",
            "bm_id": (a.get("business") or {}).get("id"),
            "name": a.get("name", ""),
            "currency": a.get("currency", ""),
            "timezone_name": a.get("timezone_name", ""),
            "account_status": status,
            "status_label": ACCOUNT_STATUS_RU.get(status, ACCOUNT_STATUS.get(status, "")),
            "disable_reason": reason,
            # Причина 0 — это «причины нет». Пропускать её как значение нельзя:
            # у живого кабинета в карточке появлялось «активен · NONE».
            "disable_label": (DISABLE_REASON_RU.get(reason,
                                                    DISABLE_REASON.get(reason, ""))
                              if reason else ""),
            "amount_spent": _minor(a.get("amount_spent")),
            "balance": _minor(a.get("balance")),
            "spend_cap": _minor(a.get("spend_cap")),
            "daily_spend_limit": _minor(a.get("adtrust_dsl")),
            "funding_type": str(funding.get("type") or ""),
            "funding_last4": digits[-4:] if len(digits) >= 4 else "",
            "funding_display": display,
            "created_time": a.get("created_time"),
            "next_bill_date": a.get("next_bill_date"),
        }

    # ---------------------------------------------------------------- #
    def _insights(self, act_id: str, since: str, until: str, level: str,
                  breakdowns: str = "", extra_fields: list[str] | None = None
                  ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "level": level,
            "time_increment": 1,
            "time_range": json.dumps({"since": since, "until": until},
                                     separators=(",", ":")),
            "fields": ",".join(INSIGHT_BASE + (extra_fields or [])),
            "limit": 500,
        }
        if breakdowns:
            params["breakdowns"] = breakdowns
        return list(self.api.paginate(f"{act_id}/insights", params))

    @staticmethod
    def _metrics(row: dict[str, Any]) -> dict[str, Any]:
        actions = _by_action_type(row.get("actions"))
        values = _by_action_type(row.get("action_values"))
        return {
            "date": row.get("date_start"),
            "spend": _dec(row.get("spend")),
            "impressions": int(_dec(row.get("impressions"))),
            "clicks": int(_dec(row.get("clicks"))),
            # Охват и клики по ссылке хранятся, а CTR/CPM/CPC — нет: они
            # производные, и посчитанные за день они не складываются в
            # правильные за неделю. Считаем их из сумм в момент показа.
            "reach": int(_dec(row.get("reach"))),
            "link_clicks": int(_dec(row.get("inline_link_clicks"))),
            "leads": _pick(actions, LEAD_KEYS),
            "purchases": _pick(actions, PURCHASE_KEYS),
            "revenue": _pick(values, PURCHASE_KEYS),
        }

    def fetch_spend(self, act_id: str, since: str, until: str,
                    currency: str = "") -> list[dict[str, Any]]:
        """Итог по кабинету за день — АВТОРИТЕТНЫЙ факт, от него считаются
        итоги маркетолога. Уровень account, без разбивок: любая разбивка
        может не сойтись с итогом, и тогда испортится главное число."""
        return [{"act_id": act_id, "currency": currency, **self._metrics(r)}
                for r in self._insights(act_id, since, until, "account")]

    def fetch_spend_countries(self, act_id: str, since: str, until: str,
                              currency: str = "") -> list[dict[str, Any]]:
        """Разбивка по фактической стране доставки — ДЕТАЛИЗАЦИЯ.

        Именно фактическая доставка, а не разбор названия кампании: парсер
        SpendTool на `AB_test_ES` возвращал `AB`, то есть молча подменял метку.
        """
        out = []
        for r in self._insights(act_id, since, until, "account",
                                breakdowns="country"):
            out.append({"act_id": act_id, "currency": currency,
                        "country": r.get("country") or "??", **self._metrics(r)})
        return out

    def fetch_spend_campaigns(self, act_id: str, since: str, until: str,
                              currency: str = "") -> list[dict[str, Any]]:
        out = []
        for r in self._insights(act_id, since, until, "campaign",
                                extra_fields=["campaign_id", "campaign_name",
                                              "objective"]):
            out.append({"act_id": act_id, "currency": currency,
                        "campaign_id": r.get("campaign_id"),
                        "campaign_name": r.get("campaign_name", ""),
                        "objective": r.get("objective", ""),
                        **self._metrics(r)})
        return out

    def fetch_campaign_entities(self, act_id: str) -> list[dict[str, Any]]:
        """Кампании как СОСТОЯНИЕ: статус, бюджеты, проблемы.

        Отдельно от `fetch_spend_campaigns`: там суточный факт, здесь текущее
        положение дел. Боту нужно именно второе — «что сейчас крутится».
        """
        found, _, _ = self.api.paginate_fields(f"{act_id}/campaigns",
                                               CAMPAIGN_FIELDS,
                                               cache_key="campaign_entity")
        out = []
        for c in found:
            out.append({
                "campaign_id": c.get("id"), "act_id": act_id,
                "name": c.get("name", ""), "status": c.get("status", ""),
                "effective_status": c.get("effective_status", ""),
                "objective": c.get("objective", ""),
                "buying_type": c.get("buying_type", ""),
                "daily_budget": _minor(c.get("daily_budget")),
                "lifetime_budget": _minor(c.get("lifetime_budget")),
                "budget_remaining": _minor(c.get("budget_remaining")),
                "bid_strategy": c.get("bid_strategy", ""),
                "issues": c.get("issues_info") or [],
                "created_time": c.get("created_time"),
            })
        return [c for c in out if c["campaign_id"]]

    def fetch_ad_entities(self, act_id: str) -> list[dict[str, Any]]:
        """Объявления с `issues_info` — это и есть реджекты с текстом причины.

        Здесь же забирается сам креатив: заголовок, текст, картинка, кнопка
        и ссылки. Отдельным запросом это тянуть незачем — Meta отдаёт креатив
        вложенным полем, а «что именно крутится» без него не ответить.
        """
        found, _, _ = self.api.paginate_fields(
            f"{act_id}/ads",
            AD_FIELDS + ["preview_shareable_link",
                         "creative{" + ",".join(CREATIVE_FIELDS) + "}"],
            cache_key="ad_entity")
        out = []
        for a in found:
            creative = a.get("creative") or {}
            out.append({
                "ad_id": a.get("id"), "act_id": act_id,
                "campaign_id": a.get("campaign_id", ""),
                "adset_id": a.get("adset_id", ""),
                "name": a.get("name", ""),
                "effective_status": a.get("effective_status", ""),
                "issues": a.get("issues_info") or [],
                "story_id": creative.get("effective_object_story_id", ""),
                "creative": self._creative_row(a, creative),
            })
        return [a for a in out if a["ad_id"]]

    @staticmethod
    def _creative_row(ad: dict[str, Any], creative: dict[str, Any]
                      ) -> dict[str, Any]:
        """Плоское представление креатива.

        Ссылка на лендинг лежит в двух местах в зависимости от того, как
        собрано объявление: у обычного — `link_url`, у сделанного из поста —
        внутри `object_story_spec`. Берём первое найденное, иначе у половины
        объявлений «куда льют» осталось бы пустым.
        """
        spec = creative.get("object_story_spec") or {}
        link_data = spec.get("link_data") or spec.get("video_data") or {}
        story_id = creative.get("effective_object_story_id", "")
        return {
            "creative_id": creative.get("id", ""),
            "title": creative.get("title") or "",
            "body": creative.get("body") or link_data.get("message") or "",
            "thumb_url": creative.get("thumbnail_url") or "",
            "link_url": (creative.get("link_url")
                         or link_data.get("link")
                         or (link_data.get("call_to_action") or {})
                         .get("value", {}).get("link") or ""),
            "permalink": (f"https://www.facebook.com/{story_id}"
                          if story_id else ""),
            # Короткая ссылка на предпросмотр: открывается без доступа
            # к кабинету, поэтому её можно переслать кому угодно.
            "preview_url": ad.get("preview_shareable_link") or "",
            "instagram_url": creative.get("instagram_permalink_url") or "",
            "cta": creative.get("call_to_action_type") or "",
            "object_type": creative.get("object_type") or "",
        }

    def fetch_spend_ads(self, act_id: str, since: str, until: str,
                        currency: str = "") -> list[dict[str, Any]]:
        """Суточный факт по объявлениям — «какое крео сколько съело»."""
        out = []
        for r in self._insights(act_id, since, until, "ad",
                                extra_fields=["ad_id", "ad_name",
                                              "campaign_id"]):
            out.append({"act_id": act_id, "currency": currency,
                        "ad_id": r.get("ad_id"),
                        "ad_name": r.get("ad_name", ""),
                        "campaign_id": r.get("campaign_id", ""),
                        **self._metrics(r)})
        return [r for r in out if r["ad_id"]]

    def fetch_breakdown(self, act_id: str, kind: str, since: str, until: str,
                        currency: str = "") -> list[dict[str, Any]]:
        """Открут в разрезе возраста, пола, плейсмента, устройства, региона."""
        field = BREAKDOWNS.get(kind)
        if not field:
            return []
        out = []
        for r in self._insights(act_id, since, until, "account",
                                breakdowns=field):
            out.append({"act_id": act_id, "currency": currency, "kind": kind,
                        "value": str(r.get(field) or "—"), **self._metrics(r)})
        return out


    # ---------------------------------------------------------------- #
    # Комментарии — модерация постов на страницах
    # ---------------------------------------------------------------- #
    def fetch_pages(self) -> list[dict[str, Any]]:
        """Страницы с их page-токенами. Без токена страницы модерация
        невозможна — комментарии живут на странице, не на кабинете."""
        out = []
        data = self.api.get("me/accounts",
                            {"fields": "id,name,access_token,tasks", "limit": 200})
        for p in data.get("data", []):
            if not p.get("access_token"):
                continue
            tasks = p.get("tasks") or []
            out.append({
                "page_id": p["id"], "name": p.get("name", ""),
                "token": p["access_token"],
                # Право удалять — задача MODERATE или полный контроль.
                "can_moderate": any(t in tasks for t in
                                    ("MODERATE", "PROFILE_PLUS_MODERATE",
                                     "MANAGE", "PROFILE_PLUS_MANAGE",
                                     "PROFILE_PLUS_FULL_CONTROL")),
            })
        return out

    def list_comments(self, object_id: str, page_token: str,
                      limit: int = 100) -> list[dict[str, Any]]:
        """Комментарии под постом (или под рекламным `story_id`)."""
        try:
            data = self.api.get_as(f"{object_id}/comments", page_token,
                                   {"fields": "id,message,from,created_time",
                                    "filter": "stream", "limit": limit})
        except GraphError:
            return []
        out = []
        for c in data.get("data", []):
            frm = c.get("from") or {}
            out.append({"comment_id": c["id"],
                        "message": c.get("message", ""),
                        "author": frm.get("name", ""),
                        "created_time": c.get("created_time", "")})
        return out

    def page_post_ids(self, page_id: str, page_token: str,
                      limit: int = 50) -> list[str]:
        """ID постов на самой странице (ФП) — свежие сверху."""
        try:
            data = self.api.get_as(f"{page_id}/posts", page_token,
                                   {"fields": "id", "limit": limit})
        except GraphError:
            return []
        return [p["id"] for p in data.get("data", []) if p.get("id")]

    def delete_comment(self, comment_id: str, page_token: str) -> bool:
        return self.api.delete_as(comment_id, page_token)


    def fetch_adset_entities(self, act_id: str) -> list[dict[str, Any]]:
        """Адсеты с аудиторией: возраст, пол, гео, плейсменты.

        Это глубина, которой не было: раньше доходили до кампаний. Аудитория
        отвечает на «на кого лью и что заходит».
        """
        found, _, _ = self.api.paginate_fields(
            f"{act_id}/adsets",
            ["id", "name", "campaign_id", "effective_status", "status",
             "optimization_goal", "billing_event", "daily_budget",
             "lifetime_budget", "targeting"],
            cache_key="adset_entity")
        # Ниже из `targeting` достаётся то, что раньше выбрасывалось: интересы,
        # аудитории, исключения, устройства, языки. Это и есть ответ на
        # «почему одна связка идёт, а вторая нет» — цифры сами по себе его
        # не дают.
        out = []
        for a in found:
            t = a.get("targeting") or {}
            geo = (t.get("geo_locations") or {}).get("countries") or []
            out.append({
                "adset_id": a.get("id"), "act_id": act_id,
                "campaign_id": a.get("campaign_id", ""),
                "name": a.get("name", ""),
                "effective_status": a.get("effective_status", ""),
                # `status` — то, чем управляем мы; `effective_status` Meta
                # считает сама. После нашей паузы адсет внутри работающей
                # кампании станет PAUSED в первом и ADSET_PAUSED во втором,
                # а чужая пауза кампании даст CAMPAIGN_PAUSED при нашем ACTIVE.
                "status": a.get("status", ""),
                "optimization_goal": a.get("optimization_goal", ""),
                "daily_budget": _minor(a.get("daily_budget")),
                "lifetime_budget": _minor(a.get("lifetime_budget")),
                "age_min": a.get("age_min") or t.get("age_min"),
                "age_max": a.get("age_max") or t.get("age_max"),
                "genders": ",".join(str(g) for g in (t.get("genders") or [])),
                "countries": ",".join(geo),
                "platforms": ",".join(t.get("publisher_platforms") or []),
                "billing_event": a.get("billing_event", ""),
                "interests": _names(t.get("flexible_spec") or t.get("interests")),
                "custom_audiences": _names(t.get("custom_audiences")),
                "excluded": _names(t.get("excluded_custom_audiences")),
                "devices": ",".join(t.get("device_platforms") or []),
                "locales": ",".join(str(x) for x in (t.get("locales") or [])),
            })
        return [a for a in out if a["adset_id"]]

    # ---------------------------------------------------------------- #
    # Контроль: кто имеет доступ, что менялось, что токен вообще умеет
    # ---------------------------------------------------------------- #
    def fetch_permissions(self) -> list[dict[str, Any]]:
        """Что этот токен реально умеет.

        Половина «сбор ничего не собрал» — не поломка, а отсутствующее
        разрешение. Без этого списка отличить одно от другого можно только
        гаданием: Meta на нехватку прав отвечает пустым набором, а не отказом.
        """
        try:
            data = self.api.get("me/permissions", {"limit": 200})
        except GraphError:
            return []
        return [{"permission": p.get("permission", ""),
                 "status": p.get("status", "")}
                for p in data.get("data", []) if p.get("permission")]

    def fetch_account_users(self, act_id: str) -> list[dict[str, Any]]:
        """Кто ещё имеет доступ к кабинету.

        «Кабинет закреплён за М16» — утверждение о нашей базе. Кто на самом
        деле может в него зайти, знает только Meta, и расхождение здесь —
        не косметика: у прежнего сотрудника мог остаться доступ.
        """
        out = []
        for edge in ("assigned_users", "users"):
            try:
                found, _, _ = self.api.paginate_fields(
                    f"{act_id}/{edge}", ["id", "name", "tasks"],
                    cache_key=f"acct_{edge}")
            except GraphError:
                continue
            for u in found:
                if not u.get("id"):
                    continue
                out.append({"user_id": str(u["id"]), "name": u.get("name", ""),
                            "tasks": ",".join(str(t) for t in (u.get("tasks") or []))})
            if out:
                break  # `assigned_users` полнее; `users` — запасной путь
        return out

    def fetch_activities(self, act_id: str, since_days: int = 30
                         ) -> list[dict[str, Any]]:
        """Журнал изменений кабинета: кто, что и когда поменял.

        Снапшот отвечает на «что стало», журнал — на «кто сделал». Для
        контроля важно второе: срезанный лимит и добавленный в кабинет
        человек выглядят в снапшоте одинаково безлично.

        `translated_event_type` — человекочитаемая формулировка от самой Meta;
        свой словарь событий заводить не будем, он устареет молча.
        """
        params = {"since": (datetime.now(timezone.utc)
                            - timedelta(days=since_days)).strftime("%Y-%m-%d"),
                  "limit": 200}
        try:
            found, _, _ = self.api.paginate_fields(
                f"{act_id}/activities",
                ["event_type", "event_time", "translated_event_type",
                 "actor_id", "actor_name", "object_id", "object_name",
                 "extra_data"],
                params, cache_key="activity")
        except GraphError:
            return []
        out = []
        for a in found:
            when = _iso_from_epoch(a.get("event_time")) or str(a.get("event_time") or "")
            # Своего id у события нет — собираем устойчивый из того, что есть,
            # иначе повторный сбор задваивал бы журнал.
            key = "|".join(str(a.get(f, "")) for f in
                           ("event_time", "event_type", "actor_id", "object_id"))
            out.append({
                "id": hashlib.sha1(f"{act_id}|{key}".encode()).hexdigest(),
                "act_id": act_id,
                "event_time": when,
                "event_type": a.get("event_type", ""),
                "event_label": a.get("translated_event_type", ""),
                "actor_id": str(a.get("actor_id") or ""),
                "actor_name": a.get("actor_name", ""),
                "object_id": str(a.get("object_id") or ""),
                "object_name": a.get("object_name", ""),
                "extra_json": json.dumps(a.get("extra_data") or "",
                                         ensure_ascii=False)[:2000],
            })
        return out

    def fetch_pixels(self, act_id: str) -> list[dict[str, Any]]:
        """Пиксели кабинета. Объясняют пустые лиды и покупки: нет пикселя —
        неоткуда взяться конверсиям, и это не ошибка сбора."""
        try:
            found, _, _ = self.api.paginate_fields(
                f"{act_id}/adspixels", ["id", "name", "last_fired_time"],
                cache_key="pixel")
        except GraphError:
            return []
        return [{"pixel_id": str(p["id"]), "act_id": act_id,
                 "name": p.get("name", ""),
                 "last_fired_at": p.get("last_fired_time")}
                for p in found if p.get("id")]

    def fetch_business_credit(self, bm_id: str) -> dict[str, Any]:
        """Кредитная линия BM. Деньги, которых нет ни в балансе кабинета,
        ни в списаниях, но которые определяют, сколько ещё можно открутить."""
        try:
            found, _, _ = self.api.paginate_fields(
                f"{bm_id}/extendedcredits",
                ["id", "legal_entity_name", "max_balance", "balance"],
                cache_key="credit")
        except GraphError:
            return {}
        for c in found:
            avail = _minor(c.get("max_balance")) - _minor(c.get("balance"))
            return {"credit_available": avail,
                    "credit_currency": str(c.get("currency") or "")}
        return {}

    # ---------------------------------------------------------------- #
    # Контроль на уровне BM: люди, страницы, пиксели
    # ---------------------------------------------------------------- #
    def fetch_business_users(self, bm_id: str) -> list[dict[str, Any]]:
        """Кто имеет доступ к бизнес-менеджеру.

        Доступ к BM сильнее доступа к кабинету: человек с ролью в BM
        дотягивается до всех кабинетов внутри, включая те, которых нет
        в нашем реестре. Три ребра, а не одно, потому что Meta считает
        разными сущностями работающего человека, приглашённого и робота,
        а с точки зрения контроля это один вопрос — «кто может войти».

        Приглашение (`pending_users`) особенно важно: доступа ещё нет,
        но он появится сам, без нашего участия, как только приглашение
        примут. Увидеть это надо до, а не после.
        """
        edges = (
            ("member", "business_users",
             ["id", "name", "email", "role", "title", "two_fac_status"]),
            ("pending", "pending_users",
             ["id", "email", "role", "expiration_time", "invite_link"]),
            ("system", "system_users",
             ["id", "name", "role", "created_time"]),
        )
        out: list[dict[str, Any]] = []
        for kind, edge, fields in edges:
            try:
                found, _, _ = self.api.paginate_fields(
                    f"{bm_id}/{edge}", fields, cache_key=f"bm_{edge}")
            except GraphError:
                continue  # часть BM закрыта для этого токена — это норма
            for u in found:
                # У приглашения своего id может не быть — ключом тогда
                # служит почта, иначе строка не запишется вовсе.
                uid = str(u.get("id") or u.get("email") or "")
                if not uid:
                    continue
                out.append({
                    "bm_id": bm_id,
                    "user_id": uid,
                    "kind": kind,
                    "name": u.get("name", ""),
                    "email": u.get("email", ""),
                    "role": str(u.get("role") or ""),
                    "title": u.get("title", ""),
                    "two_factor": str(u.get("two_fac_status") or ""),
                    "expires_at": u.get("expiration_time") or "",
                })
        return out

    def fetch_business_pages(self, bm_id: str) -> list[dict[str, Any]]:
        """Страницы бизнеса — свои и клиентские.

        Страница это лицо открута и точка, куда приходят жалобы. Своя
        (`owned_pages`) и клиентская (`client_pages`) различаются не
        косметически: клиентскую владелец может забрать в любой момент,
        и реклама встанет вместе с ней.
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for kind, edge in (("owned", "owned_pages"), ("client", "client_pages")):
            try:
                found, _, _ = self.api.paginate_fields(
                    f"{bm_id}/{edge}",
                    ["id", "name", "link", "verification_status", "fan_count",
                     "is_published", "category"],
                    cache_key=f"bm_{edge}")
            except GraphError:
                continue
            for p in found:
                pid = str(p.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                out.append({
                    "page_id": pid,
                    "bm_id": bm_id,
                    "kind": kind,
                    "name": p.get("name", ""),
                    "link": p.get("link", ""),
                    "category": p.get("category", ""),
                    "verification_status": str(p.get("verification_status") or ""),
                    "fan_count": int(p.get("fan_count") or 0),
                    "is_published": int(bool(p.get("is_published", True))),
                })
        return out

    def fetch_business_pixels(self, bm_id: str) -> list[dict[str, Any]]:
        """Пиксели бизнеса. Дополняют пиксели кабинета: пиксель живёт
        в BM, а к кабинетам привязывается — и пиксель, не привязанный
        ни к одному, из `{act}/adspixels` не виден вообще."""
        try:
            found, _, _ = self.api.paginate_fields(
                f"{bm_id}/owned_pixels", ["id", "name", "last_fired_time"],
                cache_key="bm_pixel")
        except GraphError:
            return []
        return [{"pixel_id": str(p["id"]), "act_id": "", "bm_id": bm_id,
                 "name": p.get("name", ""),
                 "last_fired_at": p.get("last_fired_time")}
                for p in found if p.get("id")]

    def fetch_ad_rules(self, act_id: str) -> list[dict[str, Any]]:
        """Автоправила кабинета.

        Правило меняет бюджет и статусы само. Журнал изменений покажет,
        что бюджет срезан, но назовёт автором робота — а почему именно
        так, знает только правило. Без этого списка любое расхождение
        «мы так не настраивали» разбирается вслепую.
        """
        try:
            found, _, _ = self.api.paginate_fields(
                f"{act_id}/adrules_library",
                ["id", "name", "status", "created_time", "updated_time",
                 "evaluation_spec", "execution_spec", "schedule_spec"],
                cache_key="ad_rule")
        except GraphError:
            return []
        out = []
        for r in found:
            if not r.get("id"):
                continue
            execution = r.get("execution_spec") or {}
            out.append({
                "rule_id": str(r["id"]),
                "act_id": act_id,
                "name": r.get("name", ""),
                "status": str(r.get("status") or ""),
                "action": str(execution.get("execution_type") or ""),
                "condition_text": _rule_condition(r.get("evaluation_spec")),
                "schedule": _rule_schedule(r.get("schedule_spec")),
                "created_time": r.get("created_time"),
                "updated_time": r.get("updated_time"),
                "spec_json": json.dumps(
                    {"evaluation": r.get("evaluation_spec"),
                     "execution": execution,
                     "schedule": r.get("schedule_spec")},
                    ensure_ascii=False)[:4000],
            })
        return out

    # ---------------------------------------------------------------- #
    # Управление: единственное место, где мы что-то МЕНЯЕМ у Meta
    # ---------------------------------------------------------------- #
    # Ниже — операции с чужими деньгами. Каждая вызывается только по явному
    # подтверждению человека, и каждая возвращает то, что РЕАЛЬНО стало,
    # а не то, что мы просили: Meta умеет принять запрос и применить его
    # частично, и верить своему намерению вместо её ответа нельзя.

    def set_campaign_status(self, campaign_id: str, active: bool) -> dict[str, Any]:
        """Остановить или запустить кампанию.

        Меняем `status`, а не `effective_status`: второе Meta считает сама,
        и кампания, запущенная нами внутри отключённого кабинета, всё равно
        останется неактивной. Управляем тем, чем можем.
        """
        self.api.post(campaign_id, {"status": "ACTIVE" if active else "PAUSED"})
        return self.campaign_state(campaign_id)

    def set_adset_status(self, adset_id: str, active: bool) -> dict[str, Any]:
        self.api.post(adset_id, {"status": "ACTIVE" if active else "PAUSED"})
        return self.adset_state(adset_id)

    def set_campaign_budget(self, campaign_id: str, daily: Any = None,
                            lifetime: Any = None) -> dict[str, Any]:
        """Сменить бюджет кампании.

        Meta принимает бюджет в минорных единицах (центах) целым числом.
        Передать 8.5 значит получить 8 центов вместо восьми с половиной
        долларов — поэтому перевод делается здесь, один раз, а не в каждом
        вызывающем месте.

        Дневной и общий бюджет взаимоисключающи: у кампании живёт ровно один,
        и попытка задать второй заканчивается отказом Meta. Какой именно —
        решает вызывающий, здесь мы не угадываем.
        """
        fields = {}
        if daily is not None:
            fields["daily_budget"] = str(int(_dec(daily) * 100))
        if lifetime is not None:
            fields["lifetime_budget"] = str(int(_dec(lifetime) * 100))
        if not fields:
            raise ValueError("бюджет не указан")
        self.api.post(campaign_id, fields)
        return self.campaign_state(campaign_id)

    def set_adset_budget(self, adset_id: str, daily: Any = None,
                         lifetime: Any = None) -> dict[str, Any]:
        fields = {}
        if daily is not None:
            fields["daily_budget"] = str(int(_dec(daily) * 100))
        if lifetime is not None:
            fields["lifetime_budget"] = str(int(_dec(lifetime) * 100))
        if not fields:
            raise ValueError("бюджет не указан")
        self.api.post(adset_id, fields)
        return self.adset_state(adset_id)

    def campaign_state(self, campaign_id: str) -> dict[str, Any]:
        """Что стало с кампанией — со слов самой Meta, а не с наших."""
        data, _, _ = self.api.get_with_fields(campaign_id, [
            "id", "name", "status", "effective_status", "daily_budget",
            "lifetime_budget", "budget_remaining"])
        return {
            "campaign_id": str(data.get("id") or campaign_id),
            "name": data.get("name", ""),
            "status": data.get("status", ""),
            "effective_status": data.get("effective_status", ""),
            "daily_budget": _minor(data.get("daily_budget")),
            "lifetime_budget": _minor(data.get("lifetime_budget")),
            "budget_remaining": _minor(data.get("budget_remaining")),
        }

    def adset_state(self, adset_id: str) -> dict[str, Any]:
        data, _, _ = self.api.get_with_fields(adset_id, [
            "id", "name", "status", "effective_status", "daily_budget",
            "lifetime_budget"])
        return {
            "adset_id": str(data.get("id") or adset_id),
            "name": data.get("name", ""),
            "status": data.get("status", ""),
            "effective_status": data.get("effective_status", ""),
            "daily_budget": _minor(data.get("daily_budget")),
            "lifetime_budget": _minor(data.get("lifetime_budget")),
        }

    def fetch_transactions(self, act_id: str, since_days: int = 45
                           ) -> list[dict[str, Any]]:
        """Фактические списания. Это НЕ insights: insights — оценка по показам,
        а здесь деньги, которые FB предъявил к оплате. Сверка с картами
        MyBroCards идёт именно по ним."""
        params = {"time_start": int(time.time()) - since_days * 86400}
        found, _, _ = self.api.paginate_fields(f"{act_id}/transactions",
                                               TRANSACTION_FIELDS, params,
                                               cache_key="transaction")
        out = []
        for t in found:
            # FB кладёт сумму в `amount` объектом. Берём
            # `total_amount_in_hundredths` — целые центы: строковое
            # `total_amount` приходит с запятой («3,00») и ломает разбор.
            money = t.get("amount") or t.get("billed_amount_details") or {}
            cents = money.get("total_amount_in_hundredths")
            amount = (_dec(cents) / 100 if cents is not None
                      else _dec(str(money.get("total_amount", "0")).replace(",", ".")))
            out.append({
                "tx_id": t.get("id"),
                "act_id": act_id,
                # `time` приходит unix-секундами — переводим в ISO, чтобы
                # сопоставлять с платежами карт по времени.
                "time": _iso_from_epoch(t.get("time")),
                "charge_type": t.get("charge_type", ""),
                "status": t.get("status", ""),
                "payment_option": t.get("payment_option", ""),
                "currency": money.get("currency", ""),
                "amount": amount,
                "tax_amount": _dec((money.get("tax_amount") or 0)),
            })
        return [t for t in out if t["tx_id"]]


ADAPTERS: dict[str, type[PlatformAdapter]] = {"fb": FacebookAdapter}


def adapter_for(platform: str, secret: str, proxy: str = "") -> PlatformAdapter:
    cls = ADAPTERS.get(platform)
    if cls is None:
        raise ValueError(f"платформа не поддерживается: {platform}")
    return cls(secret, proxy)  # type: ignore[call-arg]


def rolling_window(days: int = 7, today: date | None = None) -> tuple[str, str]:
    """Окно перезабора.

    Meta уточняет цифры прошедших дней около месяца, поэтому собирать день один
    раз нельзя. Но и тянуть `date_preset=maximum` каждый прогон, как делал
    старый коллектор, нельзя тем более — это вся история за раз и сожжённая квота.
    Середина: ежедневно последние 7 дней, раз в неделю — 28.
    """
    end = today or date.today()
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


__all__ = ["PlatformAdapter", "FacebookAdapter", "adapter_for", "rolling_window",
           "AuthError", "TransientError", "GraphError"]
