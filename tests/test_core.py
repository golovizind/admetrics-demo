#!/usr/bin/env python3
"""
Тесты ядра Admetrics.

Проверяется ровно то, на чём ошиблись обе исходные заготовки, — потому что
эти ошибки молчаливые: система не падает, она просто показывает неверные деньги.

  1. математика циклов и совместимость с нумерацией команды;
  2. атрибуция по владельцу НА ДАТУ, включая передачу кабинета;
  3. отсутствие двойного счёта при шаренном кабинете;
  4. пересчёт валют в рубли и честное поведение при отсутствии курса;
  5. спенд без владельца не исчезает из отчёта;
  6. идемпотентность upsert;
  7. детектор пересечения интервалов владения.
"""
from __future__ import annotations

import os
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import sys
import tempfile
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet

# Окружение должно быть готово до импорта store — он читает его на импорте.
os.environ["ADMETRICS_KEY"] = Fernet.generate_key().decode()
os.environ["ADMETRICS_DB"] = os.path.join(tempfile.mkdtemp(), "test.sqlite3")
os.environ.pop("DATABASE_URL", None)
os.environ["ADMETRICS_BASE_CURRENCY"] = "RUB"

from core import cycles, store  # noqa: E402

FAILS: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'OK  ' if condition else 'FAIL'} {label}")
    if not condition:
        FAILS.append(label)


def eq(actual, expected, label: str) -> None:
    check(actual == expected, f"{label} (получено {actual!r}, ждали {expected!r})"
          if actual != expected else label)


# --------------------------------------------------------------------------- #
def test_cycles() -> None:
    print("\n--- 1. Циклы ---")
    eq(cycles.cycle_of(date(2026, 8, 3)), 239, "якорь = цикл 239")
    eq(cycles.cycle_of(date(2026, 8, 9)), 239, "воскресенье того же цикла")
    eq(cycles.cycle_of(date(2026, 8, 10)), 240, "следующий понедельник = 240")
    eq(cycles.cycle_of(date(2026, 8, 12)), 240, "сегодня в цикле 240")
    eq(cycles.cycle_of(date(2026, 7, 27)), 238, "неделя до якоря = 238")
    eq(cycles.cycle_of(date(2026, 7, 20)), 237, "две недели до якоря = 237")

    start, end = cycles.cycle_range(240)
    eq((start.isoformat(), end.isoformat()), ("2026-08-10", "2026-08-16"),
       "границы цикла 240")
    check(start.weekday() == 0 and end.weekday() == 6, "цикл идёт пн→вс")

    # Обратимость на длинном горизонте — ловит ошибку знака при делении.
    ok = all(cycles.cycle_of(cycles.cycle_range(c)[0]) == c
             for c in range(100, 400))
    check(ok, "cycle_of(начало цикла) == цикл на 300 циклах подряд")

    eq(cycles.cycles_between(date(2026, 7, 27), date(2026, 8, 12)),
       [238, 239, 240], "циклы за период")
    eq(cycles.last_cycles(3, today=date(2026, 8, 12)), [238, 239, 240],
       "последние три цикла")
    eq(cycles.parse_cycle("c240"), 240, "разбор 'c240'")
    eq(cycles.parse_cycle("ц240"), 240, "разбор 'ц240'")
    eq(cycles.parse_cycle("prev", today=date(2026, 8, 12)), 239, "разбор 'prev'")
    check("Цикл 240" in cycles.label(240), "подпись цикла")


def test_secrets() -> None:
    print("\n--- 2. Шифрование секретов ---")
    secret = "eyJ0b2tlbiI6ICJFQUFCIn0="
    check(store.decrypt(store.encrypt(secret)) == secret, "round-trip контейнера")
    check(store.encrypt(secret) != secret.encode(), "в базе лежит шифротекст")
    eq(store.decrypt(None), "", "None → пустая строка, а не падение")


def test_money() -> None:
    print("\n--- 3. Деньги в Decimal ---")
    eq(store.minor_to_major("123456"), Decimal("1234.56"), "центы → рубли/доллары")
    eq(store.dec(0.1) + store.dec(0.2), Decimal("0.3"),
       "0.1 + 0.2 == 0.3 (float дал бы 0.30000000000000004)")
    eq(store.dec(None), Decimal("0"), "None → 0")
    eq(store.dec("мусор"), Decimal("0"), "мусор → 0, без исключения")


def seed() -> dict[str, str]:
    """Базовый набор: два маркетолога, три кабинета, курсы."""
    store.init_db()
    anna = store.create_marketer("Анна", login="anna", marketer_id="m_anna")
    boris = store.create_marketer("Борис", login="boris", marketer_id="m_boris")

    for act, name, cur in (("act_1", "Кабинет 1", "USD"),
                           ("act_2", "Кабинет 2", "USD"),
                           ("act_3", "Кабинет 3", "EUR")):
        store.upsert_ad_account({"act_id": act, "platform": "fb", "name": name,
                                 "currency": cur, "account_status": 1})
    # Курс: 1 USD = 80 RUB, 1 EUR = 90 RUB — на все дни цикла 240.
    for day in ("2026-08-10", "2026-08-11", "2026-08-12",
                "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16"):
        store.set_rate(day, "USD", "80", source="test")
        store.set_rate(day, "EUR", "90", source="test")
    return {"anna": anna, "boris": boris}


def spend(rows_in: list[dict]) -> None:
    """Записать факт так, как это делает боевой сборщик: итог по кабинету
    в spend_daily И разбивку по странам в spend_country_daily.

    Итог считается по spend_daily, поэтому тесты обязаны писать обе таблицы —
    иначе они проверяли бы не то, что работает на проде.
    """
    store.upsert_spend_country(rows_in)
    totals: dict[tuple, dict] = {}
    for r in rows_in:
        key = (r["date"], r["act_id"])
        acc = totals.setdefault(key, {"date": r["date"], "act_id": r["act_id"],
                                      "currency": r.get("currency", ""),
                                      "spend": Decimal(0), "clicks": 0})
        # У кабинета одна валюта — это инвариант Meta. Если тест пишет
        # разные валюты в один кабинет, он проверяет несуществующий сценарий.
        if acc["currency"] != r.get("currency", ""):
            raise AssertionError(
                f"кабинет {r['act_id']} не может иметь две валюты: "
                f"{acc['currency']} и {r.get('currency')}")
        acc["spend"] += store.dec(r.get("spend", 0))
        acc["clicks"] += int(r.get("clicks", 0) or 0)
    store.upsert_spend_daily([{**v, "spend": str(v["spend"])} for v in totals.values()])


def test_ownership() -> None:
    print("\n--- 4. Владение кабинетом ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")
    eq(store.owner_at("act_1", "2026-08-05"), ids["anna"], "владелец до передачи")

    # Передача кабинета в середине цикла 240.
    store.set_owner("act_1", ids["boris"], "2026-08-13")
    eq(store.owner_at("act_1", "2026-08-12"), ids["anna"],
       "до передачи — прежний владелец")
    eq(store.owner_at("act_1", "2026-08-13"), ids["boris"],
       "с даты передачи — новый владелец")

    intervals = store.owner_intervals("act_1")
    eq(len(intervals), 2, "два интервала владения")
    eq(intervals[0]["until"], "2026-08-12", "прежний интервал закрыт днём раньше")
    check(intervals[1]["until"] is None, "новый интервал открыт")
    eq(store.check_owner_overlaps(), [], "пересечений интервалов нет")

    eq(store.owner_at("act_2", "2026-08-12"), None,
       "кабинет без владельца отдаёт None, а не падает")


def test_no_double_count() -> None:
    print("\n--- 5. Шаренный кабинет: нет двойного счёта ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")

    # Кабинет виден двум личкам — сбор отработал дважды за один день.
    # Ключ факта без владельца, поэтому это ОДНА строка, а не две.
    for _ in range(2):
        spend([
            {"date": "2026-08-10", "act_id": "act_1", "country": "BR",
             "currency": "USD", "spend": "100", "clicks": 50},
        ])

    with store.connect() as conn:
        cur = conn.execute("SELECT COUNT(*) AS n, SUM(spend) AS s "
                           "FROM spend_country_daily WHERE act_id='act_1'")
        row = dict(cur.fetchall()[0])
    eq(int(row["n"]), 1, "повторный сбор не создал вторую строку")
    eq(store.dec(row["s"]), Decimal("100"), "спенд не удвоился")

    tree = store.founder_tree(240)
    eq(tree["total_base"], Decimal("8000"), "100 USD × 80 = 8000 RUB")


def test_founder_tree() -> None:
    print("\n--- 6. Витрина маркетолог → цикл → гео ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")
    store.set_owner("act_3", ids["boris"], "2026-08-01")
    # act_1 передаётся Борису с 13-го — внутри цикла 240.
    store.set_owner("act_1", ids["boris"], "2026-08-13")

    spend([
        # Анна владеет act_1 до 12-го включительно
        {"date": "2026-08-10", "act_id": "act_1", "country": "BR",
         "currency": "USD", "spend": "100", "clicks": 10},
        {"date": "2026-08-11", "act_id": "act_1", "country": "MX",
         "currency": "USD", "spend": "50", "clicks": 5},
        {"date": "2026-08-12", "act_id": "act_1", "country": "BR",
         "currency": "USD", "spend": "25", "clicks": 3},
        # с 13-го тот же кабинет уже у Бориса
        {"date": "2026-08-13", "act_id": "act_1", "country": "BR",
         "currency": "USD", "spend": "200", "clicks": 20},
        # свой кабинет Бориса, в евро
        {"date": "2026-08-10", "act_id": "act_3", "country": "DE",
         "currency": "EUR", "spend": "10", "clicks": 1},
    ])

    tree = store.founder_tree(240)
    by_name = {m["name"]: m for m in tree["marketers"]}

    # Анна: (100 + 50 + 25) USD × 80 = 14 000
    eq(by_name["Анна"]["spend_base"], Decimal("14000"), "спенд Анны до передачи")
    # Борис: 200 USD × 80 + 10 EUR × 90 = 16 000 + 900 = 16 900
    eq(by_name["Борис"]["spend_base"], Decimal("16900"),
       "спенд Бориса после передачи + свой кабинет")
    eq(tree["total_base"], Decimal("30900"), "итог по команде")

    anna_geo = {g["country"]: g for g in by_name["Анна"]["cycles"][0]["geo"]}
    eq(anna_geo["BR"]["spend_base"], Decimal("10000"), "гео BR у Анны")
    eq(anna_geo["MX"]["spend_base"], Decimal("4000"), "гео MX у Анны")
    eq([g["country"] for g in by_name["Анна"]["cycles"][0]["geo"]], ["BR", "MX"],
       "гео отсортированы по убыванию спенда")

    eq(by_name["Борис"]["cycles"][0]["cycle"], 240, "цикл проставлен")
    check("10–16 авг" in by_name["Борис"]["cycles"][0]["label"], "подпись цикла в витрине")

    # Сумма витрины обязана сойтись с сырой суммой фактов, пересчитанной вручную.
    with store.connect() as conn:
        cur = conn.execute("SELECT currency, SUM(spend) AS s FROM spend_country_daily "
                           "GROUP BY currency")
        raw = {r["currency"]: store.dec(r["s"]) for r in [dict(x) for x in cur.fetchall()]}
    manual = raw.get("USD", Decimal(0)) * 80 + raw.get("EUR", Decimal(0)) * 90
    eq(tree["total_base"], manual, "витрина сходится с сырой суммой фактов")


def test_geo_cannot_corrupt_marketer_total() -> None:
    print("\n--- 6b. Гео-разбивка не портит итог маркетолога ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")

    # Итог кабинета — 100 USD. Разбивка по странам отдала только 80:
    # Meta не разнесла часть открута (обычная ситуация с breakdowns).
    store.upsert_spend_daily([
        {"date": "2026-08-10", "act_id": "act_1", "currency": "USD",
         "spend": "100", "clicks": 40},
    ])
    store.upsert_spend_country([
        {"date": "2026-08-10", "act_id": "act_1", "country": "BR",
         "currency": "USD", "spend": "60", "clicks": 25},
        {"date": "2026-08-10", "act_id": "act_1", "country": "MX",
         "currency": "USD", "spend": "20", "clicks": 8},
    ])

    tree = store.founder_tree(240)
    anna = tree["marketers"][0]
    eq(anna["spend_base"], Decimal("8000"),
       "итог маркетолога = 100 USD × 80, а не 80 USD × 80")
    eq(tree["total_base"], Decimal("8000"), "итог по команде тоже верен")

    geo = {g["country"]: g for g in anna["cycles"][0]["geo"]}
    eq(geo["BR"]["spend_base"], Decimal("4800"), "гео BR = 60 USD")
    eq(geo["MX"]["spend_base"], Decimal("1600"), "гео MX = 20 USD")
    eq(geo["—"]["spend_base"], Decimal("1600"),
       "неразнесённый остаток 20 USD показан отдельной строкой")

    leaves = sum((g["spend_base"] for g in geo.values()), Decimal(0))
    eq(leaves, anna["spend_base"], "сумма листьев равна итогу родителя")

    eq(len(tree["geo_gaps"]), 1, "расхождение зафиксировано в geo_gaps")
    eq(store.dec(tree["geo_gaps"][0]["residual"]), Decimal("20"),
       "величина расхождения записана")

    eq(anna["cycles"][0]["clicks"], 40,
       "клики цикла взяты из итога кабинета, а не из суммы по странам (33)")


def test_geo_absent_entirely() -> None:
    print("\n--- 6c. Гео-разбивки нет вовсе ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")
    store.upsert_spend_daily([
        {"date": "2026-08-10", "act_id": "act_1", "currency": "USD", "spend": "100"},
    ])
    tree = store.founder_tree(240)
    eq(tree["total_base"], Decimal("8000"),
       "итог маркетолога есть даже без разбивки по странам")
    geo = {g["country"]: g for g in tree["marketers"][0]["cycles"][0]["geo"]}
    eq(list(geo), ["—"], "весь спенд в строке «не разнесено»")
    eq(tree["geo_gaps"], [], "это не расхождение — разбивки просто не собирали")


def test_geo_without_totals() -> None:
    print("\n--- 6d. Гео есть, итога кабинета нет ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")
    store.upsert_spend_country([
        {"date": "2026-08-10", "act_id": "act_1", "country": "BR",
         "currency": "USD", "spend": "70"},
    ])
    tree = store.founder_tree(240)
    eq(tree["total_base"], Decimal("5600"), "спенд не потерян: 70 USD × 80")
    eq(tree["geo_without_totals"], [("act_1", "2026-08-10")],
       "но помечен как гео без авторитетного итога")


def test_missing_rate() -> None:
    print("\n--- 7. Отсутствующий курс не превращается в ноль ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")
    store.set_owner("act_2", ids["anna"], "2026-08-01")
    # У кабинета одна валюта, поэтому сценарий строится на двух кабинетах:
    # act_1 в долларах (курс есть), act_2 в реалах (курса нет вовсе).
    store.upsert_ad_account({"act_id": "act_2", "platform": "fb",
                             "name": "Кабинет 2", "currency": "BRL"})
    spend([
        {"date": "2026-08-10", "act_id": "act_1", "country": "BR",
         "currency": "USD", "spend": "100"},
    ])
    spend([
        {"date": "2026-08-10", "act_id": "act_2", "country": "AR",
         "currency": "BRL", "spend": "500"},
    ])
    tree = store.founder_tree(240)
    eq(tree["total_base"], Decimal("8000"), "в базовую сумму вошло только конвертируемое")
    eq(tree["missing_rates"], [("BRL", "2026-08-10")], "дырка по курсу видна явно")

    geo = {g["country"]: g for g in tree["marketers"][0]["cycles"][0]["geo"]}
    eq(geo["AR"]["unconverted"], Decimal("500"),
       "неконвертированный спенд сохранён отдельно, а не потерян")
    eq(geo["AR"]["spend_base"], Decimal("0"), "и не подмешан в базовую сумму")
    eq(tree["marketers"][0]["unconverted"], Decimal("500"),
       "неконвертированное поднято на уровень маркетолога — фаундер это увидит")


def test_rub_passthrough() -> None:
    print("\n--- 8. Базовая валюта не требует курса ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")
    spend([
        {"date": "2026-08-10", "act_id": "act_1", "country": "RU",
         "currency": "RUB", "spend": "777"},
    ])
    tree = store.founder_tree(240)
    eq(tree["total_base"], Decimal("777"), "рубли проходят без конвертации")
    eq(tree["missing_rates"], [], "и не считаются дыркой в курсах")


def test_unassigned() -> None:
    print("\n--- 9. Спенд без владельца не исчезает ---")
    seed()  # владельцев не назначаем вовсе
    spend([
        {"date": "2026-08-10", "act_id": "act_2", "country": "BR",
         "currency": "USD", "spend": "42"},
    ])
    tree = store.founder_tree(240)
    eq(len(tree["marketers"]), 1, "одна группа в витрине")
    eq(tree["marketers"][0]["marketer_id"], store.UNASSIGNED, "группа «без владельца»")
    eq(tree["total_base"], Decimal("3360"), "42 USD × 80 = 3360, спенд в итоге есть")

    orphans = store.unassigned_spend("2026-08-10", "2026-08-16")
    eq(len(orphans), 1, "кабинет-сирота найден отдельным запросом")
    eq(orphans[0]["act_id"], "act_2", "именно act_2")
    eq(orphans[0]["name"], "Кабинет 2", "с названием из реестра")


def test_idempotency_and_registry() -> None:
    print("\n--- 10. Идемпотентность и реестр ---")
    seed()
    store.upsert_ad_account({"act_id": "act_1", "platform": "fb",
                             "name": "Переименован", "currency": "USD",
                             "account_status": 2, "disable_reason": 3,
                             "daily_spend_limit": "250", "funding_last4": "4242"})
    with store.connect() as conn:
        cur = conn.execute("SELECT * FROM ad_accounts WHERE act_id='act_1'")
        row = dict(cur.fetchall()[0])
    eq(row["name"], "Переименован", "upsert обновил, а не задвоил")
    eq(row["funding_last4"], "4242", "карта записана — ключ сверки с MyBroCards")
    check(row["first_seen_at"] <= row["last_seen_at"], "first_seen не затёрт")

    store.save_snapshot("act_1", "2026-08-11",
                        {"account_status": 1, "daily_spend_limit": "250",
                         "funding_last4": "4242"})
    store.save_snapshot("act_1", "2026-08-12",
                        {"account_status": 2, "disable_reason": 3,
                         "daily_spend_limit": "50", "funding_last4": "4242"})
    diff = store.snapshot_diff("act_1", "2026-08-12", "2026-08-11")
    check("account_status" in diff, "диф видит смену статуса → алерт «бан»")
    check("daily_spend_limit" in diff, "диф видит срез лимита → алерт «порезали лимит»")
    check("funding_last4" not in diff, "неизменившееся поле не шумит")


def test_overlap_detector() -> None:
    print("\n--- 11. Детектор пересечения интервалов владения ---")
    ids = seed()
    store.set_owner("act_1", ids["anna"], "2026-08-01")
    store.set_owner("act_1", ids["boris"], "2026-08-13")
    eq(store.check_owner_overlaps(), [], "штатный сценарий пересечений не даёт")

    # Портим данные в обход set_owner — как это сделала бы миграция или правка руками.
    with store.connect() as conn:
        conn.execute("INSERT INTO ad_account_owner (act_id, marketer_id, since, until) "
                     "VALUES ('act_1', 'm_anna', '2026-08-05', NULL)")
    overlaps = store.check_owner_overlaps()
    check(len(overlaps) > 0, "порча данных обнаружена")

    # И показываем, во что она обходится: спенд задваивается.
    spend([
        {"date": "2026-08-10", "act_id": "act_1", "country": "BR",
         "currency": "USD", "spend": "100"},
    ])
    tree = store.founder_tree(240)
    check(tree["total_base"] > Decimal("8000"),
          f"пересечение действительно задваивает спенд ({tree['total_base']} вместо 8000)")


def test_fx_gaps() -> None:
    print("\n--- 12. Уплотнение курсов на выходные ---")
    store.init_db()
    store.set_rate("2026-08-14", "GBP", "100", source="cbr")  # пятница
    filled = store.fill_rate_gaps("GBP", "2026-08-14", "2026-08-17")
    check(filled >= 3, f"курс перенесён вперёд на дни без котировки ({filled})")
    with store.connect() as conn:
        cur = conn.execute("SELECT rate_to_base FROM fx_rates "
                           "WHERE currency='GBP' AND date='2026-08-16'")
        got = dict(cur.fetchall()[0])
    eq(store.dec(got["rate_to_base"]), Decimal("100"), "воскресный курс = пятничный")


def main() -> int:
    for fn in (test_cycles, test_secrets, test_money, test_ownership,
               test_no_double_count, test_founder_tree,
               test_geo_cannot_corrupt_marketer_total, test_geo_absent_entirely,
               test_geo_without_totals, test_missing_rate,
               test_rub_passthrough, test_unassigned,
               test_idempotency_and_registry, test_overlap_detector, test_fx_gaps):
        # Каждый тест начинает с чистой базы — состояние не течёт между сценариями.
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


if __name__ == "__main__":
    sys.exit(main())
