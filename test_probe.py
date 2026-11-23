#!/usr/bin/env python3
"""
Тест probe.py на замоканном Graph API.

Проверяем главное, что легко сломать:
  1. bisect по полям находит рабочее подмножество, когда часть полей «не существует»;
  2. разбор контейнера FBTOOL (base64 JSON с cookies/ua);
  3. пагинация не зацикливается;
  4. minor→major конвертация центов;
  5. отчёт собирается целиком, даже когда половина эндпоинтов отдаёт ошибки.
"""
from __future__ import annotations

import base64
import json

import probe

# Поля, которые «поддерживает» наш фейковый Graph. Всё остальное — 400 code=100.
SUPPORTED = {
    "me": {"id", "name", "first_name", "last_name", "email", "link"},
    "business": {"id", "name", "created_time", "verification_status", "primary_page"},
    "adaccount": {"id", "account_id", "name", "account_status", "disable_reason",
                  "currency", "amount_spent", "balance", "spend_cap",
                  "adtrust_dsl", "funding_source_details", "business",
                  "timezone_name", "created_time"},
    "transaction": {"id", "time", "charge_type", "billed_amount_details",
                    "payment_option", "status"},
    "campaign": {"id", "name", "status", "effective_status", "objective"},
    "ad": {"id", "name", "effective_status", "issues_info"},
    "page": {"id", "name", "category"},
    "pixel": {"id", "name"},
    "businessuser": {"id", "name", "email", "role"},
}

CALLS: list[str] = []


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400
        self.headers = {
            "x-business-use-case-usage": json.dumps(
                {"123": [{"type": "ads_management", "call_count": 12,
                          "total_cputime": 5, "total_time": 7,
                          "estimated_time_to_regain_access": 0}]})
        }
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _entity_for(path: str) -> str:
    if path.endswith("/transactions"):
        return "transaction"
    if path.endswith("/campaigns"):
        return "campaign"
    if path.endswith("/ads"):
        return "ad"
    if path.endswith("/adspixels"):
        return "pixel"
    if path.endswith("/business_users"):
        return "businessuser"
    if "adaccounts" in path or path.endswith("_ad_accounts") or path.startswith("act_"):
        return "adaccount"
    if path.endswith("businesses"):
        return "business"
    if path.endswith("/owned_pages") or path.endswith("me/accounts"):
        return "page"
    if path.endswith("/me") or path.endswith("/me/"):
        return "me"
    return "me"


def _row(entity: str, index: int) -> dict:
    base = {"id": f"{entity}_{index}", "name": f"{entity.title()} {index}"}
    if entity == "adaccount":
        base |= {
            "id": f"act_10000{index}", "account_id": f"10000{index}",
            "account_status": 1 if index % 3 else 2,
            "disable_reason": 0 if index % 3 else 3,
            "currency": "USD", "amount_spent": str(123456 + index * 1000),
            "balance": "0", "spend_cap": "0", "adtrust_dsl": "25000",
            "funding_source_details": {"id": "1", "type": "CREDIT_CARD",
                                       "display_string": f"Visa *{4000 + index}"},
            "business": {"id": "biz_1", "name": "Test BM"},
            "timezone_name": "America/Los_Angeles",
        }
    if entity == "transaction":
        base |= {"time": "2026-08-01T10:00:00+0000", "charge_type": "campaign_charge",
                 "status": "Completed", "payment_option": "credit_card",
                 "billed_amount_details": {"currency": "USD", "total_amount": "5000"}}
    if entity == "ad":
        base |= {"effective_status": "DISAPPROVED",
                 "issues_info": [{"level": "AD", "error_code": 1815869,
                                  "error_summary": "Ad rejected"}]}
    return base


def fake_get(self, url, params=None, timeout=None):
    """Имитация Graph API: валидирует поля и отдаёт коллекции с пагинацией."""
    CALLS.append(url)
    params = params or {}
    path = url.split("/v21.0/", 1)[-1] if "/v21.0/" in url else url
    entity = _entity_for(path)

    requested = [f for f in str(params.get("fields", "")).split(",") if f]
    unknown = [f for f in requested if f not in SUPPORTED.get(entity, set())]
    if unknown:
        return FakeResponse(
            {"error": {"message": f"(#100) Tried accessing nonexisting field "
                                  f"({unknown[0]}) on node type",
                       "code": 100, "type": "OAuthException"}}, status=400)

    # Эндпоинты, которых «нет» — проверяем, что probe их переживает.
    if any(k in path for k in ("extendedcredits", "pending_users", "permissions", "app")):
        if path.endswith("permissions"):
            return FakeResponse({"data": [
                {"permission": "ads_read", "status": "granted"},
                {"permission": "ads_management", "status": "granted"},
                {"permission": "business_management", "status": "declined"}]})
        return FakeResponse({"error": {"message": "Unsupported get request.",
                                       "code": 100}}, status=400)

    # Одиночный узел
    if path == "me":
        return FakeResponse({f: _row("me", 1).get(f, f"v_{f}") for f in requested})

    count = {"business": 2, "adaccount": 4, "transaction": 3,
             "campaign": 5, "ad": 6}.get(entity, 2)
    limit = int(params.get("limit", 100) or 100)
    data = [{f: _row(entity, i).get(f) for f in requested} or _row(entity, i)
            for i in range(1, min(count, limit) + 1)]
    return FakeResponse({"data": data, "paging": {}})


def build_container() -> str:
    payload = {"token": "EAAB_fake_token",
               "ua": "Mozilla/5.0 (Macintosh) TestUA",
               "cookies": [{"name": "c_user", "value": "100012345",
                            "domain": ".facebook.com", "path": "/"},
                           {"name": "xs", "value": "abc", "domain": ".facebook.com"}]}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def check(condition: bool, label: str) -> bool:
    print(f"  {'OK  ' if condition else 'FAIL'} {label}")
    return condition


def main() -> int:
    import requests
    requests.Session.get = fake_get  # type: ignore[method-assign]

    ok = True
    print("\n--- 1. Разбор контейнера FBTOOL ---")
    container = build_container()
    sess, token, meta = probe.build_session(container, proxy="socks5://u:p@h:1080")
    ok &= check(token == "EAAB_fake_token", "токен извлечён")
    ok &= check(meta["cookies"] == 2, f"cookies перенесены: {meta['cookies']}")
    ok &= check(meta["ua"].endswith("TestUA"), "user-agent перенесён")
    ok &= check(sess.proxies.get("https", "").startswith("socks5://"), "прокси проставлен")

    print("\n--- 2. Голый токен EAA ---")
    _, token2, meta2 = probe.build_session("EAA_plain_token")
    ok &= check(token2 == "EAA_plain_token" and meta2["kind"] == "token",
                "голый токен принят")

    print("\n--- 3. Конвертация минорных единиц ---")
    ok &= check(probe.minor_to_major("123456") == 1234.56, "123456 центов → 1234.56")
    ok &= check(probe.minor_to_major(None) == 0.0, "None → 0.0")
    ok &= check(probe.minor_to_major("х") == 0.0, "мусор → 0.0")

    print("\n--- 4. Bisect по полям (главная механика) ---")
    api = probe.Graph(sess, token, "v21.0")
    data, alive, dead = api.get_with_fields("me", probe.ME_FIELDS)
    ok &= check(set(alive) == SUPPORTED["me"],
                f"живые поля найдены точно: {sorted(alive)}")
    ok &= check(set(dead) == set(probe.ME_FIELDS) - SUPPORTED["me"],
                f"мёртвые поля отсеяны: {sorted(dead)}")
    ok &= check(data.get("id") is not None, "данные вернулись, а не пустой dict")

    print("\n--- 5. Bisect на широком наборе ad_account (56 полей) ---")
    _, a_alive, a_dead = api.get_with_fields("act_1", probe.AD_ACCOUNT_FIELDS)
    ok &= check(set(a_alive) == SUPPORTED["adaccount"] & set(probe.AD_ACCOUNT_FIELDS),
                f"живых {len(a_alive)} из {len(probe.AD_ACCOUNT_FIELDS)}")
    ok &= check(len(a_alive) + len(a_dead) == len(probe.AD_ACCOUNT_FIELDS),
                "ни одно поле не потеряно при делении")

    print("\n--- 6. Разбор заголовков лимитов ---")
    ok &= check(api.worst_usage_pct() >= 12, f"утилизация посчитана: {api.worst_usage_pct()}")

    print("\n--- 7. Полный прогон probe() ---")
    CALLS.clear()
    report = probe.probe(container, version="v21.0", max_accounts=3, verbose=False)
    ok &= check(len(report["ad_accounts"]) >= 4,
                f"кабинеты собраны: {len(report['ad_accounts'])}")
    ok &= check(len(report["finance"]) == 3, f"финсрез по 3 кабинетам: {len(report['finance'])}")
    ok &= check(report["finance"][0]["amount_spent"] > 0, "amount_spent сконвертирован")
    ok &= check(report["finance"][0]["funding"].startswith("Visa *"),
                f"карта извлечена: {report['finance'][0]['funding']}")
    ok &= check(any(r["status"] == "DISABLED" for r in report["finance"]),
                "статус DISABLED расшифрован словарём")
    ok &= check(report["permissions_granted"] == ["ads_management", "ads_read"],
                f"права отфильтрованы по granted: {report['permissions_granted']}")
    ok &= check(len(report["errors"]) > 0,
                f"недоступные эндпоинты записаны в errors, а не уронили прогон "
                f"({len(report['errors'])})")
    ok &= check(report["api_calls"] == len(CALLS),
                f"счётчик запросов совпал: {report['api_calls']}")

    print("\n--- 8. Резюме рендерится ---")
    text = probe.summarize(report)
    ok &= check("РЕЗЮМЕ РАЗВЕДКИ" in text, "заголовок на месте")
    ok &= check("Visa *4001" in text, "карты попали в резюме")
    ok &= check("DISABLED" in text, "статусы попали в резюме")
    print("\n" + text)

    ok &= test_comments_probe()

    print("\n" + ("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if ok else "ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok else 1



def test_comments_probe():
    """Проверка цепочки прав на комментарии: три исхода, которые нас волнуют."""
    import probe as pr
    ok = True
    print("\n--- 9. Разведка комментариев ---")

    class FakeGraph:
        def __init__(self, pages, comments=None, creatives=None, version="v21.0"):
            self._pages, self._comments = pages, comments
            self._creatives = creatives if creatives is not None else [
                {"id": "cr_1", "effective_object_story_id": "1122334455_9988776655"}]
            self.session, self.version, self.calls = None, version, 0
        def paginate(self, path, params=None, max_pages=20):
            if path == "me/accounts":
                return iter(self._pages)
            if path.endswith("/adcreatives"):
                return iter(self._creatives)
            if path.endswith("/comments"):
                if self._comments is None:
                    raise pr.GraphError("нет доступа", code=10)
                return iter(self._comments)
            return iter([])

    real_graph = pr.Graph
    accounts = [{"id": "act_1"}]

    # 1. Страницы без токена — модерация невозможна.
    pr.Graph = lambda *a, **k: FakeGraph([{"id": "1122334455", "name": "P"}])
    rep = {"permissions_granted": ["pages_read_engagement"]}
    res = pr.probe_comments(FakeGraph([{"id": "1122334455", "name": "P"}]), accounts, rep)
    ok &= check(res["page_tokens"] == 0 and "невозможна" in res["verdict"],
                f"нет page-токена → {res['verdict'][:50]}")

    # 2. Токен есть, комментарии читаются, но нет права на модерацию.
    pages = [{"id": "1122334455", "name": "P", "access_token": "PAGE_TOK"}]
    g = FakeGraph(pages, comments=[{"id": "c1", "message": "текст"}])
    pr.Graph = lambda *a, **k: g
    rep = {"permissions_granted": ["pages_read_engagement"]}
    res = pr.probe_comments(g, accounts, rep)
    ok &= check(bool(res["comments_readable"]) and not res["can_moderate"],
                f"читаем, но не модерируем → {res['verdict'][:60]}")

    # 3. Полный доступ.
    rep = {"permissions_granted": ["pages_read_engagement",
                                   "pages_manage_engagement"]}
    res = pr.probe_comments(g, accounts, rep)
    ok &= check(bool(res["can_moderate"])
                and "модерация доступны" in res["verdict"],
                f"полный доступ → {res['verdict'][:60]}")

    pr.Graph = real_graph
    return ok


if __name__ == "__main__":
    raise SystemExit(main())
