#!/usr/bin/env python3
"""
Смоук-тесты веба и коллектора на замоканном Graph API.

Веб проверяется на то, что он не падает и показывает правильные числа —
в частности, что итог маркетолога берётся из авторитетного источника, а
проблемы (спенд без владельца, дырки в курсах) видны на витрине, а не
спрятаны в лог.

Коллектор проверяется на разделении ошибок: мёртвая личка помечается
`needs_reconnect`, временная ошибка статус НЕ портит.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, timedelta

from cryptography.fernet import Fernet

os.environ["ADMETRICS_KEY"] = Fernet.generate_key().decode()
os.environ["ADMETRICS_DB"] = os.path.join(tempfile.mkdtemp(), "web.sqlite3")
os.environ.pop("DATABASE_URL", None)
os.environ["ADMETRICS_BASE_CURRENCY"] = "RUB"
os.environ["ADMIN_USER"] = "founder"
os.environ["ADMIN_PASS"] = "founder-pass"
os.environ["CRON_TOKEN"] = "cron-secret"

FAILS: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'OK  ' if condition else 'FAIL'} {label}")
    if not condition:
        FAILS.append(label)


# --------------------------------------------------------------------------- #
# Фейковый Graph API
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.ok = payload, status, status < 400
        self.headers = {"x-app-usage": json.dumps(
            {"call_count": 5, "total_cputime": 3, "total_time": 4})}
        self.text = json.dumps(payload)

    def json(self):
        return self._p


SCENARIO = {"mode": "ok"}


def fake_get(self, url, params=None, timeout=None):
    params = params or {}
    path = url.split("/v21.0/", 1)[-1] if "/v21.0/" in url else url
    fields = [f for f in str(params.get("fields", "")).split(",") if f]

    if SCENARIO["mode"] == "dead":
        return FakeResponse({"error": {"message": "Session expired",
                                       "code": 190, "error_subcode": 463}}, 400)
    if SCENARIO["mode"] == "transient":
        return FakeResponse({"error": {"message": "Please reduce the amount",
                                       "code": 17}}, 400)

    if path == "me":
        return FakeResponse({"id": "fb_user_1", "name": "Тестовая личка"})
    if path == "me/businesses":
        return FakeResponse({"data": [{f: {"id": "bm_1", "name": "BM 1"}.get(f, "")
                                       for f in fields}], "paging": {}})
    if "adaccounts" in path or path.endswith("_ad_accounts"):
        row = {"id": "act_777", "account_id": "777", "name": "Тестовый кабинет",
               "account_status": 1, "disable_reason": 0, "currency": "USD",
               "timezone_name": "UTC", "amount_spent": "120000",
               "balance": "0", "spend_cap": "0", "adtrust_dsl": "25000",
               "funding_source_details": {"type": "CREDIT_CARD",
                                          "display_string": "Visa *4242"},
               "business": {"id": "bm_1", "name": "BM 1"}}
        if path.endswith("_ad_accounts"):
            return FakeResponse({"data": [], "paging": {}})
        return FakeResponse({"data": [{f: row.get(f) for f in fields}], "paging": {}})
    if path.endswith("/insights"):
        base = {"date_start": DAY, "spend": "100",
                "impressions": "1000", "clicks": "40",
                "actions": [{"action_type": "lead", "value": "3"}],
                "account_id": "777"}
        if params.get("level") == "campaign":
            return FakeResponse({"data": [{**base, "campaign_id": "camp_1",
                                           "campaign_name": "BR | оффер",
                                           "objective": "OUTCOME_LEADS"}],
                                 "paging": {}})
        if params.get("breakdowns") == "country":
            return FakeResponse({"data": [
                {**base, "spend": "60", "clicks": "25", "country": "BR"},
                {**base, "spend": "20", "clicks": "8", "country": "MX"}],
                "paging": {}})
        return FakeResponse({"data": [base], "paging": {}})
    if path.endswith("/campaigns"):
        return FakeResponse({"data": [{f: {
            "id": "camp_1", "name": "BR | оффер", "status": "ACTIVE",
            "effective_status": "ACTIVE", "objective": "OUTCOME_LEADS",
            "buying_type": "AUCTION", "daily_budget": "5000",
            "lifetime_budget": "0", "budget_remaining": "3000",
            "bid_strategy": "LOWEST_COST", "issues_info": None,
            "created_time": OWNED_FROM, "updated_time": DAY,
        }.get(f) for f in fields}], "paging": {}})
    if path.endswith("/ads"):
        row = {
            "id": "ad_1", "name": "крео 1", "status": "ACTIVE",
            "effective_status": "DISAPPROVED", "campaign_id": "camp_1",
            "adset_id": "set_1", "updated_time": DAY,
            "issues_info": [{"level": "AD", "error_code": 1815869,
                             "error_summary": "Нарушение рекламной политики"}],
        }
        out = {f: row.get(f) for f in fields if not f.startswith("creative")}
        # Meta на запрос `creative{...}` отвечает ключом `creative`, а не
        # полным текстом запрошенного поля — мок обязан вести себя так же.
        if any(f.startswith("creative") for f in fields):
            out["creative"] = {"effective_object_story_id": "1122334455_998877"}
        return FakeResponse({"data": [out], "paging": {}})
    if path.endswith("/transactions"):
        return FakeResponse({"data": [{
            "id": "tx_1", "time": f"{DAY}T10:00:00+0000",
            "charge_type": "campaign_charge", "status": "Completed",
            "payment_option": "credit_card",
            "billed_amount_details": {"currency": "USD",
                                      "total_amount": "95",
                                      "tax_amount": "5"}}], "paging": {}})
    return FakeResponse({"data": [], "paging": {}})


import requests  # noqa: E402

requests.Session.get = fake_get  # type: ignore[method-assign]

import collector  # noqa: E402
from core import cycles, store, ui  # noqa: E402

# --------------------------------------------------------------------------- #
# Даты фикстур
# --------------------------------------------------------------------------- #
# Привязаны к ТЕКУЩЕМУ циклу, а не к календарю. Прибитые к августу даты
# зеленели ровно одну неделю, а потом набор «ломался» без единой правки кода —
# и отличить это от настоящей поломки можно было только руками.
CYCLE = cycles.current_cycle()
SINCE, UNTIL = cycles.cycle_bounds_iso(CYCLE)
CYCLE_DAYS = [(date.fromisoformat(SINCE) + timedelta(days=i)).isoformat()
              for i in range(7)]
# День, за который есть спенд. Не сегодняшний: отдельная проверка требует,
# чтобы период «сегодня» не показывал этих денег.
DAY = next(d for d in CYCLE_DAYS if d != date.today().isoformat())
# Владение начинается раньше цикла, иначе спенд первого дня останется ничьим.
OWNED_FROM = cycles.cycle_bounds_iso(CYCLE - 1)[0]
PREV_SINCE = cycles.cycle_bounds_iso(CYCLE - 2)[0]


def setup() -> str:
    store.init_db()
    mid = store.create_marketer("Анна", login="anna",
                                password_hash=_hash("secret123"))
    store.add_identity(mid, "EAA_fake", proxy="", label="профиль 1")
    for day in CYCLE_DAYS:
        store.set_rate(day, "USD", "80", source="test")
    return mid


def _hash(password: str) -> str:
    import base64
    import hashlib
    import os as _os
    salt = _os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return (f"pbkdf2$200000${base64.b64encode(salt).decode()}"
            f"${base64.b64encode(dk).decode()}")


# --------------------------------------------------------------------------- #
def test_collector_ok() -> None:
    print("\n--- 1. Коллектор: успешный проход ---")
    SCENARIO["mode"] = "ok"
    mid = setup()
    res = collector.run(since=SINCE, until=UNTIL)

    check(res["accounts"] == 1, f"кабинет попал в реестр ({res['accounts']})")
    check(res["spend_rows"] == 1, f"строка авторитетного спенда ({res['spend_rows']})")
    check(res["geo_rows"] == 2, f"две строки гео ({res['geo_rows']})")
    check(res["tx_rows"] == 1, f"транзакция сохранена ({res['tx_rows']})")

    with store.connect() as conn:
        acc = store.rows(conn.execute("SELECT * FROM ad_accounts"))[0]
        snap = store.rows(conn.execute("SELECT * FROM ad_account_snapshots"))
        ident = store.rows(conn.execute("SELECT * FROM identities"))[0]
        access = store.rows(conn.execute("SELECT * FROM ad_account_access"))
    check(acc["funding_last4"] == "4242", "last4 карты извлечён из «Visa *4242»")
    check(store.dec(acc["daily_spend_limit"]) == 250,
          f"adtrust_dsl переведён из центов ({acc['daily_spend_limit']})")
    check(store.dec(acc["amount_spent"]) == 1200, "amount_spent переведён из центов")
    check(len(snap) == 1, "суточный снапшот записан")
    check(ident["status"] == "ok", "личка помечена живой")
    check(len(access) == 1, "доступ личики к кабинету зафиксирован")

    store.set_owner("act_777", mid, OWNED_FROM)
    tree = store.founder_tree(CYCLE)
    check(tree["total_base"] == store.dec("8000"),
          f"итог 100 USD × 80 = 8000 из spend_daily, а не 80×80 ({tree['total_base']})")
    check(len(tree["geo_gaps"]) == 1,
          "расхождение гео (60+20 против 100) зафиксировано")


def test_collector_dead_identity() -> None:
    print("\n--- 2. Коллектор: мёртвая личка ---")
    SCENARIO["mode"] = "ok"
    setup()
    SCENARIO["mode"] = "dead"
    collector.run(since=SINCE, until=UNTIL)
    with store.connect() as conn:
        ident = store.rows(conn.execute("SELECT * FROM identities"))[0]
        log = store.rows(conn.execute("SELECT * FROM identity_status_log"))
    check(ident["status"] == "needs_reconnect", "личка помечена needs_reconnect")
    check("истёк" in (ident["last_error"] or ""),
          f"причина расшифрована по субкоду 463: {ident['last_error']!r}")
    check(len(log) >= 1, "переход статуса записан в историю")


def test_collector_transient_does_not_kill() -> None:
    print("\n--- 3. Коллектор: временная ошибка не убивает личку ---")
    SCENARIO["mode"] = "ok"
    setup()
    collector.run(since=SINCE, until=UNTIL)
    SCENARIO["mode"] = "transient"
    collector.run(since=SINCE, until=UNTIL)
    with store.connect() as conn:
        ident = store.rows(conn.execute("SELECT * FROM identities"))[0]
    check(ident["status"] == "ok",
          f"после лимита 17 личка осталась живой ({ident['status']})")


def test_web() -> None:
    print("\n--- 4. Веб ---")
    SCENARIO["mode"] = "ok"
    mid = setup()
    collector.run(since=SINCE, until=UNTIL)
    store.set_owner("act_777", mid, OWNED_FROM)

    from fastapi.testclient import TestClient
    import app as web
    client = TestClient(web.app)

    r = client.get("/healthz")
    check(r.status_code == 200 and r.json()["ok"], "/healthz отвечает")
    check(r.json()["base_currency"] == "RUB", "базовая валюта в healthz — рубль")

    r = client.get("/", auth=("founder", "founder-pass"))
    check(r.status_code == 200, "витрина фаундера открывается")
    check("Анна" in r.text, "маркетолог показан на витрине")
    check("BR" in r.text and "MX" in r.text, "гео показаны как детализация")
    check("RUB" in r.text, "суммы в базовой валюте")

    r = client.get("/", auth=("founder", "wrong"))
    check(r.status_code == 401, "неверный пароль фаундера отклонён")

    r = client.get("/accounts", auth=("founder", "founder-pass"))
    check(r.status_code == 200 and "act_777" in r.text, "реестр активов открывается")
    check("4242" in r.text, "карта видна в реестре")

    r = client.post("/login", data={"login": "anna", "password": "secret123"},
                    follow_redirects=False)
    check(r.status_code == 303, "вход маркетолога проходит")
    r = client.get("/me")
    check(r.status_code == 200 and "профиль 1" in r.text,
          "кабинет показывает личку по метке")
    check("Кабинет" in r.text or "act_777" in r.text, "кабинеты маркетолога видны")

    r = client.post("/login", data={"login": "anna", "password": "nope"})
    check(r.status_code == 401, "неверный пароль маркетолога отклонён")

    r = client.get("/cron?token=wrong")
    check(r.status_code == 403, "cron защищён токеном")

    # Вторая личка ДОБАВЛЯЕТСЯ, а не заменяет первую — главное отличие от прототипа.
    client.post("/login", data={"login": "anna", "password": "secret123"})
    client.post("/me/identity", data={"secret": "EAA_second_token",
                                      "label": "профиль 2", "proxy": ""},
                follow_redirects=False)
    with store.connect() as conn:
        idents = store.rows(conn.execute("SELECT * FROM identities"))
    check(len(idents) == 2, f"у маркетолога стало две личики ({len(idents)})")

    r = client.post("/me/identity", data={"secret": "мусор", "label": "", "proxy": ""})
    check(r.status_code == 400, "мусор вместо токена отклонён")


def test_web_shows_problems() -> None:
    print("\n--- 5. Витрина показывает проблемы, а не прячет ---")
    SCENARIO["mode"] = "ok"
    setup()
    collector.run(since=SINCE, until=UNTIL)
    # Владельца намеренно НЕ назначаем.

    from fastapi.testclient import TestClient
    import app as web
    client = TestClient(web.app)
    r = client.get("/", auth=("founder", "founder-pass"))
    check("без владельца" in r.text.lower(),
          "спенд без владельца вынесен на витрину")
    check("Без владельца" in r.text, "и показан отдельной группой в таблице")


def main() -> int:
    for fn in (test_collector_ok, test_collector_dead_identity,
               test_collector_transient_does_not_kill, test_web,
               test_web_shows_problems, test_admin_pages,
               test_admin_identities, test_marketer_identity_controls,
               test_admin_revoke, test_store_api_complete, test_campaigns_and_rejects,
               test_account_change_alerts, test_reconciliation,
               test_bot_helpers, test_pending_payments, test_buttons):
        if os.path.exists(os.environ["ADMETRICS_DB"]):
            os.remove(os.environ["ADMETRICS_DB"])
        fn()

    print("\n" + "=" * 70)
    if FAILS:
        print(f"ПАДЕНИЙ: {len(FAILS)}")
        for f in FAILS:
            print(f"  · {f}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0



def test_admin_pages() -> None:
    """Админка — единственный путь завести данные на хостинге без шелла."""
    print("\n--- 6. Админ-страницы ---")
    SCENARIO["mode"] = "ok"
    setup()
    collector.run(since=SINCE, until=UNTIL)

    from fastapi.testclient import TestClient
    import app as web
    client = TestClient(web.app)
    auth = ("founder", "founder-pass")

    r = client.get("/admin", auth=auth)
    check(r.status_code == 200, "админка открывается")
    check("Завести маркетолога" in r.text, "форма создания маркетолога есть")
    check("act_777" in r.text, "кабинет доступен для назначения владельца")

    check(client.get("/admin").status_code == 401, "админка закрыта без пароля")

    r = client.post("/admin/marketer",
                    data={"name": "Борис", "login": "boris", "password": "",
                          "role": "marketer"},
                    auth=auth, follow_redirects=False)
    check(r.status_code == 303, "маркетолог создаётся через веб")
    names = [m["name"] for m in store.list_marketers()]
    check("Борис" in names, f"и появился в списке: {names}")

    boris = next(m for m in store.list_marketers() if m["name"] == "Борис")
    r = client.post("/admin/owner",
                    data={"act_id": "act_777", "marketer_id": boris["id"],
                          "since": OWNED_FROM},
                    auth=auth, follow_redirects=False)
    check(r.status_code == 303, "владелец назначается через веб")
    check(store.owner_at("act_777", DAY) == boris["id"],
          "владение записалось на нужную дату")

    r = client.post("/admin/owner",
                    data={"act_id": "act_777", "marketer_id": boris["id"],
                          "since": "не дата"}, auth=auth, follow_redirects=False)
    check("kind=bad" in r.headers.get("location", ""), "кривая дата отклонена")

    r = client.post("/admin/fx", data={"currency": "EUR", "rate": "90",
                                       "since": SINCE, "until": UNTIL},
                    auth=auth, follow_redirects=False)
    check(r.status_code == 303, "курс проставляется на период")
    with store.connect() as conn:
        n = store.rows(conn.execute(
            "SELECT COUNT(*) AS n FROM fx_rates WHERE currency='EUR'"))[0]["n"]
    check(int(n) == 7, f"курс лёг на все 7 дней периода ({n})")

    # Пароль маркетолога хешируется, а не шифруется — проверяем, что вход работает
    # и что в базе лежит именно хеш.
    r = client.post("/admin/password",
                    data={"marketer_id": boris["id"], "password": "новый-пароль"},
                    auth=auth, follow_redirects=False)
    check(r.status_code == 303, "пароль меняется через веб")
    with store.connect() as conn:
        row = store.rows(conn.execute(store._q(
            "SELECT password_hash FROM marketers WHERE id = ?"), (boris["id"],)))[0]
    check(row["password_hash"].startswith("pbkdf2$"), "в базе pbkdf2-хеш")
    check("новый-пароль" not in row["password_hash"], "пароль в базе не хранится")
    check(store.verify_password("новый-пароль", row["password_hash"]),
          "и сверяется корректно")



def test_admin_identities() -> None:
    """Админ заводит личики сам — без входа под маркетолога."""
    print("\n--- 7. Личики через админку ---")
    SCENARIO["mode"] = "ok"
    setup()

    from fastapi.testclient import TestClient
    import app as web
    client = TestClient(web.app)
    auth = ("founder", "founder-pass")
    anna = store.list_marketers()[0]

    r = client.get("/admin", auth=auth)
    check("Добавить личку" in r.text, "форма добавления личики есть в админке")
    check("профиль 1" in r.text, "существующие личики показаны списком")

    # Ключевое: добавление НЕ требует сессии маркетолога.
    r = client.post("/admin/identity",
                    data={"marketer_id": anna["id"], "secret": "EAA_admin_added",
                          "label": "заведена админом", "proxy": "socks5://h:1"},
                    auth=auth, follow_redirects=False)
    check(r.status_code == 303, "личка добавлена без входа под маркетолога")
    idents = store.list_identities()
    check(len(idents) == 2, f"личек стало две ({len(idents)})")
    added = [i for i in idents if i["label"] == "заведена админом"]
    check(len(added) == 1, "новая личка привязана к выбранному маркетологу")
    check(added[0]["marketer_name"] == "Анна", "владелец проставлен верно")

    # Секрет доехал зашифрованным и расшифровывается коллектором.
    live = [i for i in store.active_identities() if i["label"] == "заведена админом"]
    check(live and live[0]["secret"] == "EAA_admin_added", "секрет сохранён и читается")
    check(live[0]["proxy"] == "socks5://h:1", "прокси сохранён")

    # Список личек не должен таскать расшифрованные секреты.
    check(all("secret" not in i for i in idents),
          "список для экрана не содержит секретов")

    r = client.post("/admin/identity", data={"marketer_id": anna["id"],
                                             "secret": "мусор", "label": "", "proxy": ""},
                    auth=auth, follow_redirects=False)
    check("kind=bad" in r.headers.get("location", ""), "мусор вместо токена отклонён")
    check(len(store.list_identities()) == 2, "и в базу не попал")

    r = client.post("/admin/identity", data={"marketer_id": "нет-такого",
                                             "secret": "EAA_x", "label": "", "proxy": ""},
                    auth=auth, follow_redirects=False)
    check("kind=bad" in r.headers.get("location", ""), "чужой marketer_id отклонён")

    # Выключение: личка остаётся, но коллектор её не берёт.
    target = added[0]["id"]
    client.post("/admin/identity/toggle", data={"identity_id": target, "enable": "0"},
                auth=auth, follow_redirects=False)
    check(all(i["id"] != target for i in store.active_identities()),
          "выключенная личка выпала из сбора")
    check(len(store.list_identities()) == 2, "но из базы не удалена")

    client.post("/admin/identity/toggle", data={"identity_id": target, "enable": "1"},
                auth=auth, follow_redirects=False)
    check(any(i["id"] == target for i in store.active_identities()),
          "включение возвращает личку в сбор")

    # Отзыв секрета: история остаётся, собирать нечем.
    store.delete_identity(target)
    check(all(i["id"] != target for i in store.active_identities()),
          "после отзыва секрета личка не собирается")
    check(len(store.list_identities()) == 2, "запись о личике сохранена")

    check(client.post("/admin/identity", data={"marketer_id": anna["id"],
                                               "secret": "EAA_y"}).status_code == 401,
          "добавление личики закрыто без пароля админа")



def test_marketer_identity_controls() -> None:
    """У маркетолога те же действия над личками, что у админа, — но только
    над своими. Подмена identity_id в форме не должна проходить."""
    print("\n--- 8. Личики в кабинете маркетолога ---")
    SCENARIO["mode"] = "ok"
    anna_id = setup()
    boris_id = store.create_marketer("Борис", login="boris",
                                     password_hash=_hash("boris-pass"))
    boris_ident = store.add_identity(boris_id, "EAA_boris", label="личка Бориса")

    from fastapi.testclient import TestClient
    import app as web
    client = TestClient(web.app)
    client.post("/login", data={"login": "anna", "password": "secret123"})

    r = client.get("/me")
    check("Выключить" in r.text, "кнопка выключения есть в кабинете")
    check("Отозвать" in r.text, "кнопка отзыва есть в кабинете")
    check("личка Бориса" not in r.text, "чужие личики в кабинете не видны")

    anna_ident = [i for i in store.list_identities(anna_id)][0]["id"]
    client.post("/me/identity/toggle",
                data={"identity_id": anna_ident, "enable": "0"},
                follow_redirects=False)
    check(store.get_identity(anna_ident)["status"] == "disabled",
          "маркетолог выключил свою личку")
    check(all(i["id"] != anna_ident for i in store.active_identities()),
          "и она выпала из сбора")

    client.post("/me/identity/toggle",
                data={"identity_id": anna_ident, "enable": "1"},
                follow_redirects=False)
    check(any(i["id"] == anna_ident for i in store.active_identities()),
          "и вернул обратно")

    # Граница доступа: Анна пытается тронуть личку Бориса.
    r = client.post("/me/identity/toggle",
                    data={"identity_id": boris_ident, "enable": "0"},
                    follow_redirects=False)
    check(r.status_code == 403, f"чужую личку выключить нельзя ({r.status_code})")
    check(store.get_identity(boris_ident)["status"] != "disabled",
          "статус чужой личики не изменился")

    r = client.post("/me/identity/revoke", data={"identity_id": boris_ident},
                    follow_redirects=False)
    check(r.status_code == 403, "чужую личку отозвать нельзя")
    check(any(i["id"] == boris_ident for i in store.active_identities()),
          "секрет чужой личики цел")

    r = client.post("/me/identity/toggle",
                    data={"identity_id": "выдуманный", "enable": "0"},
                    follow_redirects=False)
    check(r.status_code == 403, "несуществующий id отклонён")

    # Свой отзыв: секрет стёрт, история осталась.
    client.post("/me/identity/revoke", data={"identity_id": anna_ident},
                follow_redirects=False)
    check(store.get_identity(anna_ident)["status"] == "revoked",
          "своя личка отозвана")
    check(all(i["id"] != anna_ident for i in store.active_identities()),
          "по отозванной личике сбор не идёт")
    check(store.get_identity(anna_ident) is not None,
          "запись о личике сохранена — связи с кабинетами не потеряны")

    client.get("/logout")
    r = client.post("/me/identity/toggle",
                    data={"identity_id": anna_ident, "enable": "1"},
                    follow_redirects=False)
    check(r.status_code == 303 and "/login" in r.headers.get("location", ""),
          "без входа кидает на форму логина")


def test_admin_revoke() -> None:
    print("\n--- 9. Отзыв личики админом ---")
    SCENARIO["mode"] = "ok"
    mid = setup()
    from fastapi.testclient import TestClient
    import app as web
    client = TestClient(web.app)
    auth = ("founder", "founder-pass")

    ident = store.list_identities(mid)[0]["id"]
    r = client.post("/admin/identity/revoke", data={"identity_id": ident},
                    auth=auth, follow_redirects=False)
    check(r.status_code == 303, "админ отзывает личку")
    check(store.get_identity(ident)["status"] == "revoked", "статус revoked")
    check(store.active_identities() == [], "сбор по ней прекращён")

    r = client.post("/admin/identity/revoke", data={"identity_id": "нет"},
                    auth=auth, follow_redirects=False)
    check("kind=bad" in r.headers.get("location", ""), "несуществующий id отклонён")



def test_store_api_complete() -> None:
    """Каждое `store.X`, которое зовёт приложение, должно существовать.

    Ловит рассинхрон между файлами: именно на нём упал прод — `app.py` уехал
    в репозиторий с вызовом `store.list_identities()`, а `core/store.py` с этой
    функцией залить забыли. Тесты тогда были зелёными, потому что локально
    оба файла были свежими.
    """
    print("\n--- 10. Полнота API store ---")
    import ast
    import pathlib
    from core import store as store_mod

    missing: list[str] = []
    checked = 0
    for name in ("app.py", "collector.py", "manage.py"):
        path = pathlib.Path(__file__).parent / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "store"):
                checked += 1
                if not hasattr(store_mod, node.attr):
                    missing.append(f"{name}: store.{node.attr}")

    check(checked > 30, f"обращений к store найдено: {checked}")
    check(not missing, "все существуют" if not missing
          else f"НЕ НАЙДЕНЫ: {sorted(set(missing))}")

    # То же для core.cycles и core.schema — они тоже зовутся из веба.
    from core import cycles as cycles_mod
    from core import schema as schema_mod
    for mod, alias in ((cycles_mod, "cycles"), (schema_mod, "schema")):
        bad = []
        tree = ast.parse((pathlib.Path(__file__).parent / "app.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == alias
                    and not hasattr(mod, node.attr)):
                bad.append(node.attr)
        check(not bad, f"все обращения к {alias} существуют"
              if not bad else f"{alias}: НЕ НАЙДЕНЫ {sorted(set(bad))}")



def test_campaigns_and_rejects() -> None:
    """Кампании, объявления и реджекты — то, чего коллектор раньше не собирал."""
    print("\n--- 11. Кампании и реджекты ---")
    SCENARIO["mode"] = "ok"
    from core import alerts
    setup()
    res = collector.run(since=SINCE, until=UNTIL)

    check(res["campaigns"] >= 1, f"кампании как сущности собраны ({res['campaigns']})")
    check(res["ads"] >= 1, f"объявления собраны ({res['ads']})")
    check(res["campaign_rows"] >= 1,
          f"суточный факт по кампаниям записан ({res['campaign_rows']})")

    with store.connect() as conn:
        camp = store.rows(conn.execute("SELECT * FROM campaigns"))[0]
        ad = store.rows(conn.execute("SELECT * FROM ads"))[0]
    check(camp["objective"] == "OUTCOME_LEADS", "цель кампании сохранена")
    check(store.dec(camp["daily_budget"]) == 50,
          f"дневной бюджет из центов ({camp['daily_budget']})")
    check(ad["story_id"] == "1122334455_998877",
          "story_id сохранён — без него не найти комментарии")
    check("политики" in (ad["issues_json"] or ""), "текст реджекта сохранён")

    rejects = [a for a in alerts.recent(50) if a["rule"] == "ad_rejected"]
    check(len(rejects) == 1, f"алерт о реджекте поднят ({len(rejects)})")
    check("политики" in (rejects[0]["body"] or ""),
          "в алерте есть причина, а не просто «отклонено»")

    # Повторный прогон не должен дублировать алерт.
    collector.run(since=SINCE, until=UNTIL)
    rejects2 = [a for a in alerts.recent(50) if a["rule"] == "ad_rejected"]
    check(len(rejects2) == 1, "повторный сбор не задублировал алерт")


def test_account_change_alerts() -> None:
    """Диф состояния кабинета: бан, лимит, карта."""
    print("\n--- 12. Алерты по кабинету ---")
    from core import alerts
    store.init_db()

    base = {"act_id": "act_1", "name": "Кабинет", "account_status": 1,
            "daily_spend_limit": "250", "funding_last4": "4242"}

    events = alerts.account_changes(None, base)
    check(events == [], "новый живой кабинет не шумит")

    events = alerts.account_changes(None, {**base, "account_status": 2,
                                            "disable_reason": 3})
    check(len(events) == 1 and events[0]["rule"] == "account_dead",
          "новый забаненный кабинет — новость")

    banned = {**base, "account_status": 2, "disable_reason": 1}
    events = alerts.account_changes(base, banned)
    check(any(e["rule"] == "account_dead" for e in events), "бан пойман")
    dead = next(e for e in events if e["rule"] == "account_dead")
    check("рекламной политики" in dead["body"],
          f"причина расшифрована: {dead['body']}")

    events = alerts.account_changes(base, {**base, "daily_spend_limit": "50"})
    check(any(e["rule"] == "dsl_down" for e in events), "срез лимита пойман")
    events = alerts.account_changes(base, {**base, "daily_spend_limit": "900"})
    check(any(e["rule"] == "dsl_up" for e in events), "подъём лимита пойман")

    events = alerts.account_changes(base, {**base, "funding_last4": "9999"})
    check(any(e["rule"] == "card_changed" for e in events), "смена карты поймана")

    events = alerts.account_changes(banned, base)
    check(any(e["rule"] == "account_alive" for e in events), "оживление поймано")

    events = alerts.account_changes(base, base)
    check(events == [], "без изменений — тишина")

    # Дедупликация на уровне записи.
    first = alerts.emit_account_changes(base, banned)
    second = alerts.emit_account_changes(base, banned)
    check(first == 1 and second == 0,
          f"один бан = один алерт (записано {first}, повтор {second})")



def test_reconciliation() -> None:
    """Сверка FB ↔ MyBroCards: порог считается по Settled без комиссий."""
    print("\n--- 13. Сверка с картами ---")
    from core import alerts
    from decimal import Decimal
    store.init_db()
    mid = store.create_marketer("Анна", login="anna")
    for act, last4, name in (("act_1", "4242", "Кабинет 1"),
                             ("act_2", "7777", "Кабинет 2")):
        store.upsert_ad_account({"act_id": act, "platform": "fb", "name": name,
                                 "currency": "USD", "account_status": 1,
                                 "funding_last4": last4})
        store.set_owner(act, mid, OWNED_FROM)
    store.upsert_spend_daily([
        {"date": DAY, "act_id": "act_1", "currency": "USD", "spend": "1000"},
        {"date": DAY, "act_id": "act_2", "currency": "USD", "spend": "500"}])

    def pay(pid, last4, amount, state="Settled", fee="0", decline=None):
        store.upsert_card_payment({
            "payment_id": pid, "card_last4": last4, "currency": "USD",
            "amount": amount, "fee": fee, "state": state, "decline": decline,
            "date": f"{DAY} 12:00:00", "user_email": "anna@x.com",
            "merchant": "Facebook", "marketer_id": mid})

    # act_1: сходится. Отклонённые и комиссии рядом — они НЕ должны попасть в порог.
    pay("p1", "4242", "1000", fee="11.30")
    pay("p2", "4242", "2", state="Declined", decline="card-blocked", fee="0.5")
    pay("p3", "4242", "2", state="Declined", decline="card-blocked", fee="0.5")
    # act_2: реальное расхождение — списано 400 против 500 открута.
    pay("p4", "7777", "400", fee="4.70")

    r = alerts.reconcile(SINCE, UNTIL)
    by_act = {row["act_id"]: row for row in r["rows"]}

    check(by_act["act_1"]["card_charged"] == Decimal("1000"),
          "отклонённые не попали в зачёт")
    check(by_act["act_1"]["diff_pct"] == 0,
          f"кабинет без расхождения даёт 0% ({by_act['act_1']['diff_pct']})")
    check(by_act["act_1"]["card_fees"] == Decimal("12.30"),
          f"комиссии посчитаны отдельно ({by_act['act_1']['card_fees']})")
    check(by_act["act_1"]["declines"] == 2, "отклонённые попытки видны отдельно")

    check(by_act["act_2"]["diff"] == Decimal("-100"), "расхождение 100 найдено")
    check(abs(by_act["act_2"]["diff_pct"] - Decimal("20")) < Decimal("0.01"),
          f"20% от большего ({by_act['act_2']['diff_pct']})")

    ids = [m["act_id"] for m in r["mismatched"]]
    check(ids == ["act_2"], f"в расхождения попал только act_2: {ids}")

    # Если бы комиссии и отклонённые шли в зачёт, act_1 дал бы ложный алерт.
    naive = (by_act["act_1"]["card_charged"] + by_act["act_1"]["card_fees"]
             + by_act["act_1"]["card_declined"])
    check(naive > Decimal("1000"),
          f"наивный подсчёт дал бы {naive} против 1000 у FB — вот почему "
          f"порог считается по Settled без fee")

    # Абсолютный минимум глушит шум на мелких кабинетах.
    store.upsert_ad_account({"act_id": "act_3", "platform": "fb", "name": "Мелкий",
                             "currency": "USD", "account_status": 1,
                             "funding_last4": "3333"})
    store.set_owner("act_3", mid, OWNED_FROM)
    store.upsert_spend_daily([{"date": DAY, "act_id": "act_3",
                               "currency": "USD", "spend": "20"}])
    pay("p5", "3333", "18")
    r2 = alerts.reconcile(SINCE, UNTIL)
    small = next(x for x in r2["rows"] if x["act_id"] == "act_3")
    check(small["diff_pct"] > 5, f"по проценту прошёл бы ({small['diff_pct']:.0f}%)")
    check("act_3" not in [m["act_id"] for m in r2["mismatched"]],
          "но абсолютный минимум его отсёк — мелочь не шумит")

    written = alerts.emit_reconciliation(SINCE, UNTIL)
    check(written >= 1, f"алерт о расхождении поднят ({written})")
    check(alerts.emit_reconciliation(SINCE, UNTIL) == 0,
          "повтор за тот же период не дублируется")


def test_bot_helpers() -> None:
    """Разбор периода, нарезка сообщений и роли."""
    print("\n--- 14. Помощники бота ---")
    os.environ["ADMIN_TG_IDS"] = "999"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test"
    import importlib
    import bot as b
    importlib.reload(b)
    store.init_db()

    since, until, label = b.parse_period("7d")
    check(since < until and "7" in label, f"7d → {since}..{until} ({label})")
    since, until, label = b.parse_period(f"c{CYCLE}")
    check((since, until) == (SINCE, UNTIL), f"c{CYCLE} → {since}..{until}")
    since, until, _ = b.parse_period(f"c{CYCLE - 2}-{CYCLE}")
    check((since, until) == (PREV_SINCE, UNTIL), "диапазон циклов")
    d, u, _ = b.parse_period("today")
    check(d == u, "today — один день")

    long_text = "\n".join(f"строка {i} " + "x" * 100 for i in range(200))
    parts = b.split_message(long_text)
    check(all(len(p) <= b.MSG_LIMIT for p in parts),
          f"каждая часть влезает в лимит ({len(parts)} частей)")
    check("\n".join(parts).replace("\n", "") == long_text.replace("\n", ""),
          "при нарезке ничего не потеряно")

    check(b.esc("<script>") == "&lt;script&gt;", "HTML экранируется")

    mid = store.create_marketer("Анна", login="anna")
    with store.connect() as conn:
        conn.execute(store._q("UPDATE marketers SET tg_id = ? WHERE id = ?"),
                     ("555", mid))
    check(b.whoami("999")["role"] == "admin", "админ из ADMIN_TG_IDS")
    check(b.whoami("555")["role"] == "marketer", "привязанный — маркетолог")
    check(b.whoami("111")["role"] == "guest", "незнакомый — гость")
    check(b.require(b.whoami("111"), "admin", "marketer") is False,
          "гость не проходит проверку роли")



def test_pending_payments() -> None:
    """Pending идёт в зачёт, а переход в финальный статус не задваивает сумму.

    Это и есть решение проблемы лага: пока платёж висит Pending, деньги уже
    захолдированы, и без их учёта сверка «за сегодня» всегда показывала бы
    недостачу.
    """
    print("\n--- 15. Pending и переходы статусов ---")
    from core import alerts
    from decimal import Decimal
    store.init_db()
    mid = store.create_marketer("Анна", login="anna")
    store.upsert_ad_account({"act_id": "act_1", "platform": "fb", "name": "Кабинет",
                             "currency": "USD", "account_status": 1,
                             "funding_last4": "4242"})
    store.set_owner("act_1", mid, OWNED_FROM)
    store.upsert_spend_daily([{"date": DAY, "act_id": "act_1",
                               "currency": "USD", "spend": "1000"}])

    def pay(pid, amount, state, fee="0"):
        store.upsert_card_payment({
            "payment_id": pid, "card_last4": "4242", "currency": "USD",
            "amount": amount, "fee": fee, "state": state,
            "date": f"{DAY} 12:00:00", "marketer_id": mid})

    def row():
        return alerts.reconcile(SINCE, UNTIL)["rows"][0]

    # 1. Половина уже Settled, половина ещё висит Pending.
    pay("p1", "600", "Settled")
    pay("p2", "400", "Pending")
    r = row()
    check(r["card_settled"] == Decimal("600"), "Settled отдельно")
    check(r["card_pending"] == Decimal("400"), "Pending отдельно")
    check(r["card_charged"] == Decimal("1000"), "в зачёт идёт сумма обоих")
    check(r["diff_pct"] == 0,
          f"с учётом pending расхождения нет ({r['diff_pct']})")

    # 2. Pending доехал до Settled — та же строка, а не вторая.
    pay("p2", "400", "Settled")
    with store.connect() as conn:
        n = store.rows(conn.execute(
            "SELECT COUNT(*) AS n FROM card_payments"))[0]["n"]
    check(int(n) == 2, f"платежей в базе по-прежнему 2, а не 3 ({n})")
    r = row()
    check(r["card_charged"] == Decimal("1000"),
          f"сумма не задвоилась после перехода ({r['card_charged']})")
    check(r["card_pending"] == Decimal("0"), "pending обнулился")
    check(r["card_settled"] == Decimal("1000"), "всё стало Settled")

    # 3. Обратный случай: pending уехал в Declined и должен выпасть.
    pay("p3", "300", "Pending")
    check(row()["card_charged"] == Decimal("1300"), "новый pending учтён")
    pay("p3", "300", "Declined")
    r = row()
    check(r["card_charged"] == Decimal("1000"),
          f"отклонённый выпал из зачёта ({r['card_charged']})")
    check(r["card_declined"] == Decimal("300"), "и перешёл в отклонённые")
    check(r["declines"] == 1, "счётчик отклонений вырос")

    # 4. Регистр статуса не должен ломать подсчёт.
    pay("p4", "50", "pending")
    check(row()["card_charged"] == Decimal("1050"),
          "статус в нижнем регистре тоже учитывается")


def test_buttons() -> None:
    """Кнопки: обход всех экранов и проверка, что доступ ограничен на сервере.

    Обход рекурсивный: из главного экрана нажимается каждая кнопка, из
    полученного — снова каждая. Так экран, до которого дошли только кнопкой,
    не может остаться непроверенным.

    Отдельно проверяется главное: `callback_data` приходит от клиента, её
    можно прислать боту руками. Поэтому чужие данные должны закрываться
    маршрутизатором, а не тем, что мы не нарисовали кнопку.
    """
    print("\n--- 16. Кнопки и навигация ---")
    SCENARIO["mode"] = "ok"
    os.environ["ADMIN_TG_IDS"] = "999"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test"
    import importlib

    from datetime import date, timedelta

    import bot as b
    importlib.reload(b)

    mid = setup()
    collector.run(since=SINCE, until=UNTIL)
    store.set_owner("act_777", mid, OWNED_FROM)
    other = store.create_marketer("Борис", login="boris")
    other_ident = store.add_identity(other, "EAA_other", label="чужая")
    with store.connect() as conn:
        conn.execute(store._q("UPDATE marketers SET tg_id = ? WHERE id = ?"),
                     ("555", mid))

    calls: list[tuple[str, dict]] = []

    def fake_api(method, **params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 1}}

    b.api = fake_api
    admin, marketer = b.whoami("999"), b.whoami("555")
    check(marketer["id"] == mid and admin["role"] == "admin", "роли разобраны")

    def press(user, data):
        calls.clear()
        note = b.route_callback(user, data, 1, 100)
        screens = [p for m, p in calls if m in ("editMessageText", "sendMessage")]
        return note, screens

    # --- обход всех достижимых экранов ------------------------------------- #
    def walk(user, data, seen, depth=0):
        if data in seen or depth > 4:
            return []
        seen.add(data)
        note, screens = press(user, data)
        if not screens:
            return [] if note else [f"{data}: пусто"]
        bad = []
        for s in screens:
            if not (s.get("text") or "").strip():
                bad.append(f"{data}: пустой текст")
            for row in (s.get("reply_markup") or {}).get("inline_keyboard", []):
                for button in row:
                    cd = button["callback_data"]
                    if len(cd.encode()) > 64:
                        bad.append(f"{data}: callback_data > 64 байт ({cd})")
                    # Действия, меняющие состояние, обходить нельзя.
                    if cd.split(":")[0] not in ("col", "it", "ir", "link",
                                                "addid", "addmk", "setown",
                                                "cancel"):
                        bad += walk(user, cd, seen, depth + 1)
        return bad

    seen_admin: set[str] = set()
    problems = walk(admin, "main", seen_admin)
    check(not problems, f"обход экранов админа: {problems or 'все живые'}")
    # Без этой проверки «обход прошёл» означало бы «обход не начинался».
    check(len(seen_admin) >= 12, f"экранов пройдено: {len(seen_admin)}")
    check({"sp:today", "mk", "rc:today", "he", "al", "a:act_777"} <= seen_admin,
          f"ключевые разделы достижимы кнопками: {sorted(seen_admin)}")
    seen_mk: set[str] = set()
    problems = walk(marketer, "main", seen_mk)
    check(not problems, f"обход экранов маркетолога: {problems or 'все живые'}")
    check({"me", f"ac:{mid}:0", f"id:{mid}", "al"} <= seen_mk,
          f"маркетолог доходит до своего: {sorted(seen_mk)}")

    # --- доступ ------------------------------------------------------------ #
    check(press(marketer, "sp:today")[0] == "Недоступно",
          "маркетолог не открывает общий спенд")
    check(press(marketer, "mk")[0] == "Недоступно", "и список маркетологов")
    check(press(marketer, "he")[0] == "Недоступно", "и состояние системы")
    check(press(marketer, "col")[0] == "Недоступно", "и запуск сбора")
    check(press(marketer, f"ac:{other}:0")[0] == "Это не ваши кабинеты",
          "чужие кабинеты закрыты, хотя кнопки на них не было")
    check(press(marketer, "ac::0")[0] == "Это не ваши кабинеты",
          "и список всех кабинетов тоже")
    note, screens = press(marketer, f"id:{other}")
    check("недоступны" in screens[0]["text"], "чужие личики закрыты")
    check(press(marketer, f"it:{other_ident}:0")[0] == "Чужая личка",
          "чужую личку нельзя выключить")
    check(press(marketer, f"ir:{other_ident}")[0] == "Чужая личка",
          "и отозвать нельзя")
    with store.connect() as conn:
        st = store.rows(conn.execute(
            store._q("SELECT status FROM identities WHERE id = ?"),
            (other_ident,)))[0]["status"]
    check(st != "revoked", f"чужой секрет остался цел ({st})")

    # --- своё работает ----------------------------------------------------- #
    own = store.list_identities(mid)[0]["id"]
    note, screens = press(marketer, f"it:{own}:0")
    check(note == "Выключена" and store.get_identity(own)["status"] == "disabled",
          "свою личку маркетолог выключает")
    note, _ = press(marketer, f"it:{own}:1")
    check(note == "Включена" and store.get_identity(own)["status"] != "disabled",
          "и включает обратно")
    note, _ = press(admin, f"ir:{own}")
    check(note == "Секрет удалён"
          and store.get_identity(own)["status"] == "revoked",
          "админ отзывает секрет")

    # --- поведение ветвей -------------------------------------------------- #
    check(press(admin, "noop")[0] == "", "счётчик страниц ничего не делает")
    check(press(admin, "zzz:1")[0] == "Кнопка устарела",
          "неизвестный код не роняет бота")
    note, _ = press(admin, f"addid:{mid}")
    check(note == "Жду мультитокен" and "999" in b._PENDING,
          "кнопка «добавить личку» переводит бота в ожидание токена")
    check(b._PENDING["999"]["marketer_id"] == mid,
          "ожидание привязано к отправителю, а не к чату")
    b._PENDING.clear()
    note, screens = press(admin, f"link:{other}")
    check(note == "Ссылка выдана" and "?start=" in screens[0]["text"],
          "ссылка-приглашение выдаётся кнопкой")

    # --- период не должен расширяться до целого цикла ---------------------- #
    # Спенд лежит на DAY, а это заведомо не сегодня. Значит период «сегодня»
    # обязан показать ноль: иначе главная кнопка админа врёт — подпись
    # «сегодня», а данные за неделю.
    _, screens = press(admin, "sp:today")
    check("Итого: 0.00" in screens[0]["text"],
          "«сегодня» — это сегодня, а не весь цикл")
    _, screens = press(admin, f"sp:c{CYCLE}")
    check("Итого: 8 000.00" in screens[0]["text"],
          f"цикл {CYCLE} показывает открут: {screens[0]['text'].splitlines()[-1]}")
    since, until, label = ui.parse_period("7d")
    check((until, label) == (date.today().isoformat(), "7 дн.")
          and since == (date.today() - timedelta(days=6)).isoformat(),
          f"7d — ровно семь дней ({since}..{until})")

    # --- кривой период не роняет бота -------------------------------------- #
    for bad in ("c-5", "c240-", "c1-2-3", "²d", "999999999d", "0d", "d", "c"):
        note, screens = press(admin, f"sp:{bad}")
        ok = screens and not note.startswith("Ошибка")
        check(ok, f"период {bad!r} не роняет экран"
                  + ("" if ok else f" → {note}"))

    # --- страницы за границей списка --------------------------------------- #
    note, screens = press(admin, "ac::999999")
    check(screens and "1/1" not in screens[0].get("text", ""),
          "страница за пределами списка не ломает экран")
    check(all(b_["callback_data"] != "ac::1000000"
              for row in screens[0]["reply_markup"]["inline_keyboard"]
              for b_ in row), "и не рисует стрелку в пустоту")

    # --- список личек: кнопка «добавить» не должна теряться ---------------- #
    for i in range(14):
        store.add_identity(other, f"EAA_{i}", label=f"личка {i}")
    _, screens = press(admin, f"id:{other}")
    codes = [b_["callback_data"]
             for row in screens[0]["reply_markup"]["inline_keyboard"]
             for b_ in row]
    check(f"addid:{other}" in codes,
          "при 15 личках кнопка «добавить личку» на месте")
    check(any(c.startswith("idp:") for c in codes), "и появилось листание")

    # --- список маркетологов листается ------------------------------------- #
    for i in range(12):
        store.create_marketer(f"Тест {i}", login=f"test{i}")
    _, screens = press(admin, "mk")
    codes = [b_["callback_data"]
             for row in screens[0]["reply_markup"]["inline_keyboard"]
             for b_ in row]
    check(any(c.startswith("mkp:") for c in codes),
          "четырнадцатый маркетолог достижим кнопкой, а не только в тексте")

    # --- отозванную личку нельзя воскресить -------------------------------- #
    burned = store.add_identity(other, "EAA_burned", label="сожжённая")
    store.delete_identity(burned)
    note, _ = press(admin, f"it:{burned}:1")
    check(store.get_identity(burned)["status"] == "revoked",
          f"отзыв секрета необратим кнопкой ({note})")

    # --- список и карточка кабинета не должны расходиться ------------------ #
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    store.set_owner("act_777", other, tomorrow)
    check(not any(a["act_id"] == "act_777" for a in ui.accounts_of(other)),
          "кабинет с будущей датой владения не показывается как «мой»")
    check(ui.accounts_of(mid) and not ui.owns_account(other, "act_777"),
          "список и проверка владения отвечают одинаково")
    store.set_owner("act_777", mid, date.today().isoformat())

    # --- команд больше нет -------------------------------------------------- #
    def msg(from_id, text):
        calls.clear()
        b.handle({"message": {"message_id": 8, "chat": {"id": from_id},
                              "from": {"id": from_id}, "text": text}})
        return calls

    for text in ("/spend", "/help", "/menu", "/collect", "просто привет"):
        got = msg(999, text)
        kbs = [p.get("reply_markup") for _, p in got if p.get("reply_markup")]
        check(len(got) == 1 and kbs and kbs[0]["inline_keyboard"],
              f"{text!r} открывает меню с кнопками, а не ответ про команды")
    import re as _re
    answer = _re.sub(r"</?[a-z]+>", "",
                     "\n".join(p.get("text", "") for _, p in msg(999, "/help")))
    check(not _re.search(r"/[a-z]{2,}", answer),
          f"в ответе не предлагается набрать команду: {answer!r}")

    # --- маркетолог заводится кнопкой + именем ------------------------------ #
    press(admin, "addmk")
    check("999" in b._PENDING and b._PENDING["999"]["kind"] == "marketer",
          "кнопка «добавить маркетолога» переводит бота в ожидание имени")
    calls.clear()
    b.handle({"message": {"message_id": 9, "chat": {"id": 999},
                          "from": {"id": 999}, "text": "Пётр"}})
    texts = "\n".join(p.get("text", "") for _, p in calls)
    created = [m for m in store.list_marketers() if m["name"] == "Пётр"]
    check(created and "?start=" in texts,
          f"маркетолог создан и сразу получил ссылку-приглашение: {bool(created)}")
    check(not b._PENDING, "ожидание снято")

    # --- владелец кабинета назначается кнопкой ------------------------------ #
    note, _ = press(admin, f"setown:act_777:{created[0]['id']}")
    check(note == "Владелец назначен"
          and store.owner_at("act_777", date.today().isoformat()) == created[0]["id"],
          f"владелец сменился нажатием ({note})")
    check(press(marketer, f"setown:act_777:{mid}")[0] == "Недоступно",
          "маркетолог не может переназначить кабинет себе")
    check(press(admin, "setown:act_nope:zzz")[0] == "Кабинет не найден",
          "несуществующий кабинет отклоняется")
    store.set_owner("act_777", mid, date.today().isoformat())

    # --- в группе опознаём отправителя, а не чат --------------------------- #
    # Иначе `/start КОД`, отправленный в группу, записал бы chat_id группы
    # как tg_id маркетолога — и его спенд, кабинеты и личики увидел бы
    # каждый, кто там пишет.
    def group_msg(from_id, text):
        calls.clear()
        b.handle({"message": {"message_id": 7, "chat": {"id": -1001},
                              "from": {"id": from_id}, "text": text}})
        return "\n".join(p.get("text", "") for _, p in calls)

    check("Анна" in group_msg(555, "/me"), "свой в группе видит своё")
    out = group_msg(777, "/me")
    check("Анна" not in out and "приглашению" in out.lower(),
          f"посторонний в той же группе не видит чужого: {out!r}")

    # --- редактирование на месте ------------------------------------------- #
    calls.clear()
    b.route_callback(admin, "main", 1, 100)
    check(calls and calls[0][0] == "editMessageText",
          "переход по кнопке правит сообщение, а не плодит новые")
    calls.clear()
    b.show(1, ui.screen_main(admin))
    check(calls and calls[0][0] == "sendMessage",
          "а команда без message_id шлёт новое")


if __name__ == "__main__":
    sys.exit(main())
