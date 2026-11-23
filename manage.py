#!/usr/bin/env python3
"""
CLI Admetrics.

    python3 manage.py init                                  создать схему
    python3 manage.py marketer add "Анна" --login anna       завести маркетолога
    python3 manage.py marketer list
    python3 manage.py identity add anna --file token.txt --proxy socks5://...
    python3 manage.py identity list [anna]
    python3 manage.py owner set act_123456 anna [--since 2026-08-01]
    python3 manage.py owner orphans                          кабинеты без владельца
    python3 manage.py accounts [--problem]                   реестр активов
    python3 manage.py fx set 2026-08-12 USD 80.5
    python3 manage.py fx fill USD 2026-08-01 2026-08-31
    python3 manage.py report [--cycle 240] [--last 3]        витрина фаундера
    python3 manage.py check                                  проверки целостности
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import date
from decimal import Decimal

from core import cycles, store
from core.schema import BASE_CURRENCY


def money(value) -> str:
    return f"{Decimal(value or 0):,.2f}".replace(",", " ")


def _find_marketer(ident: str) -> dict:
    with store.connect() as conn:
        cur = conn.execute(store._q(
            "SELECT * FROM marketers WHERE id = ? OR login = ? OR name = ?"),
            (ident, ident, ident))
        found = store.rows(cur)
    if not found:
        raise SystemExit(f"маркетолог не найден: {ident}")
    return found[0]


# --------------------------------------------------------------------------- #
def cmd_init(_: argparse.Namespace) -> None:
    store.init_db()
    print(f"Схема создана. Базовая валюта: {BASE_CURRENCY}.")


def cmd_marketer_add(args: argparse.Namespace) -> None:
    store.init_db()
    password = args.password or secrets.token_urlsafe(9)
    mid = store.create_marketer(args.name, login=args.login,
                                password_hash=store.hash_password(password),
                                tg_id=args.tg or "", role=args.role,
                                team=args.team or "")
    print(f"Маркетолог: {args.name}\n  id     : {mid}\n  логин  : {args.login}\n"
          f"  пароль : {password}\n  роль   : {args.role}")
    print("Пароль показан один раз — в базе лежит только хеш.")


def cmd_marketer_list(_: argparse.Namespace) -> None:
    with store.connect() as conn:
        cur = conn.execute("""
            SELECT m.*, COUNT(i.id) AS identities
              FROM marketers m LEFT JOIN identities i ON i.marketer_id = m.id
             GROUP BY m.id ORDER BY m.name
        """)
        for m in store.rows(cur):
            flag = "" if m["active"] else "  [выключен]"
            print(f"{m['name']:<24} {m['login'] or '—':<14} {m['role']:<10} "
                  f"личек: {m['identities']}{flag}")


def cmd_identity_add(args: argparse.Namespace) -> None:
    store.init_db()
    marketer = _find_marketer(args.marketer)
    secret = (open(args.file, encoding="utf-8").read().strip()
              if args.file else (args.secret or sys.stdin.read()).strip())
    if not secret:
        raise SystemExit("пустой секрет")
    iid = store.add_identity(marketer["id"], secret, proxy=args.proxy or "",
                             label=args.label or "", platform=args.platform)
    print(f"Личка добавлена: {iid} → {marketer['name']}")
    print("Секрет зашифрован. Проверить: python3 collector.py --registry-only")


def cmd_identity_list(args: argparse.Namespace) -> None:
    sql = """
        SELECT i.id, i.label, i.platform, i.status, i.last_error,
               i.last_ok_at, m.name AS marketer
          FROM identities i JOIN marketers m ON m.id = i.marketer_id
    """
    params: tuple = ()
    if args.marketer:
        marketer = _find_marketer(args.marketer)
        sql += " WHERE i.marketer_id = ?"
        params = (marketer["id"],)
    sql += " ORDER BY m.name, i.label"
    with store.connect() as conn:
        for i in store.rows(conn.execute(store._q(sql), params)):
            mark = {"ok": "✓", "needs_reconnect": "✗"}.get(i["status"], "?")
            print(f"{mark} {i['marketer']:<20} {(i['label'] or i['id']):<20} "
                  f"{i['status']:<16} {i['last_error'][:40]}")


def cmd_owner_set(args: argparse.Namespace) -> None:
    marketer = _find_marketer(args.marketer)
    since = args.since or date.today().isoformat()
    store.set_owner(args.act_id, marketer["id"], since, note=args.note or "")
    print(f"{args.act_id} → {marketer['name']} с {since}")
    intervals = store.owner_intervals(args.act_id)
    for i in intervals:
        print(f"  {i['since']} .. {i['until'] or 'сейчас'}  {i['marketer_id']}")


def cmd_owner_orphans(args: argparse.Namespace) -> None:
    since, until = cycles.cycle_bounds_iso(args.cycle or cycles.current_cycle())
    orphans = store.unassigned_spend(since, until)
    if not orphans:
        print("Кабинетов со спендом без владельца нет.")
        return
    print(f"Спенд без владельца за {since}..{until}:")
    for o in orphans:
        print(f"  {o['act_id']:<22} {(o['name'] or '—')[:28]:<28} "
              f"{money(o['spend'])} {o['currency']}")
    print("\nНазначить: python3 manage.py owner set <act_id> <маркетолог>")


def cmd_accounts(args: argparse.Namespace) -> None:
    sql = "SELECT * FROM ad_accounts"
    if args.problem:
        sql += " WHERE account_status IS NOT NULL AND account_status <> 1"
    sql += " ORDER BY amount_spent DESC"
    with store.connect() as conn:
        found = store.rows(conn.execute(sql))
    if not found:
        print("Реестр пуст. Соберите: python3 collector.py --registry-only")
        return
    print(f"{'Кабинет':<24}{'Название':<26}{'Статус':<18}{'Лимит/день':>12}"
          f"{'Карта':>8}")
    for a in found:
        print(f"{a['act_id']:<24}{(a['name'] or '—')[:24]:<26}"
              f"{(a['status_label'] or '—'):<18}"
              f"{money(a['daily_spend_limit']):>12}"
              f"{(a['funding_last4'] or '—'):>8}")
    print(f"\nВсего: {len(found)}")


def cmd_fx_set(args: argparse.Namespace) -> None:
    store.set_rate(args.date, args.currency, args.rate, source="manual")
    print(f"{args.date}  1 {args.currency.upper()} = {args.rate} {BASE_CURRENCY}")


def cmd_fx_fill(args: argparse.Namespace) -> None:
    filled = store.fill_rate_gaps(args.currency, args.since, args.until)
    print(f"Заполнено дней переносом курса: {filled}")


def cmd_report(args: argparse.Namespace) -> None:
    if args.last:
        span = cycles.last_cycles(args.last)
        tree = store.founder_tree(span[0], span[-1])
    else:
        cycle = args.cycle or cycles.current_cycle()
        tree = store.founder_tree(cycle)

    print(f"\n{'=' * 72}")
    print(f"ВИТРИНА · {tree['since']} .. {tree['until']} · "
          f"базовая валюта {tree['base_currency']}")
    print(f"{'=' * 72}")

    for m in tree["marketers"]:
        print(f"\n■ {m['name']:<28} {money(m['spend_base']):>16} "
              f"{tree['base_currency']}")
        if m["unconverted"]:
            print(f"  (+ {money(m['unconverted'])} без курса — см. ниже)")
        for c in m["cycles"]:
            print(f"   └ {c['label']:<34} {money(c['spend_base']):>14}")
            for g in c["geo"][:args.top]:
                share = (g["spend_base"] / c["spend_base"] * 100
                         if c["spend_base"] else 0)
                print(f"       {g['country']:<6} {money(g['spend_base']):>14}"
                      f"  {share:5.1f}%")

    print(f"\n{'-' * 72}")
    print(f"ИТОГО: {money(tree['total_base'])} {tree['base_currency']}")

    if tree["missing_rates"]:
        print(f"\n⚠ Нет курса ({len(tree['missing_rates'])}): "
              + ", ".join(f"{c} на {d}" for c, d in tree["missing_rates"][:8]))
        print("  Эти суммы НЕ вошли в итог. Заполнить: manage.py fx set …")
    if tree["geo_gaps"]:
        print(f"\n⚠ Гео-разбивка не сошлась с итогом кабинета: "
              f"{len(tree['geo_gaps'])} случаев — показаны строкой «—».")
    if tree["geo_without_totals"]:
        print(f"\n⚠ Гео без итога кабинета: {len(tree['geo_without_totals'])} "
              f"(кабинет/дата) — соберите статистику уровня account.")


def cmd_check(_: argparse.Namespace) -> None:
    """Проверки целостности — то, что ломается молча."""
    problems = 0

    overlaps = store.check_owner_overlaps()
    if overlaps:
        problems += 1
        print(f"✗ Пересечение интервалов владения: {len(overlaps)}. "
              f"Это ЗАВЫШАЕТ спенд в отчёте.")
        for o in overlaps[:5]:
            print(f"    {o['act_id']}: {o['s1']}..{o['u1'] or '∞'} и "
                  f"{o['s2']}..{o['u2'] or '∞'}")
    else:
        print("✓ Интервалы владения не пересекаются")

    since, until = cycles.cycle_bounds_iso(cycles.current_cycle())
    orphans = store.unassigned_spend(since, until)
    if orphans:
        problems += 1
        print(f"✗ Кабинетов со спендом без владельца: {len(orphans)}")
    else:
        print("✓ У всего спенда текущего цикла есть владелец")

    tree = store.founder_tree(cycles.current_cycle())
    if tree["missing_rates"]:
        problems += 1
        print(f"✗ Нет курса для {len(tree['missing_rates'])} пар валюта/дата — "
              f"суммы не вошли в итог")
    else:
        print("✓ Курсы есть на все даты со спендом")

    with store.connect() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM identities WHERE status='needs_reconnect'")
        dead = int(store.rows(cur)[0]["n"])
    if dead:
        print(f"! Личек требуют переподключения: {dead}")
    else:
        print("✓ Все личики живы")

    print("\nПроблем: " + ("нет" if not problems else str(problems)))


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Управление Admetrics")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    m = sub.add_parser("marketer").add_subparsers(dest="sub", required=True)
    ma = m.add_parser("add")
    ma.add_argument("name")
    ma.add_argument("--login", required=True)
    ma.add_argument("--password")
    ma.add_argument("--tg")
    ma.add_argument("--role", default="marketer",
                    choices=["marketer", "lead", "admin"])
    ma.add_argument("--team")
    ma.set_defaults(fn=cmd_marketer_add)
    m.add_parser("list").set_defaults(fn=cmd_marketer_list)

    i = sub.add_parser("identity").add_subparsers(dest="sub", required=True)
    ia = i.add_parser("add")
    ia.add_argument("marketer")
    ia.add_argument("--secret")
    ia.add_argument("--file")
    ia.add_argument("--proxy")
    ia.add_argument("--label")
    ia.add_argument("--platform", default="fb")
    ia.set_defaults(fn=cmd_identity_add)
    il = i.add_parser("list")
    il.add_argument("marketer", nargs="?")
    il.set_defaults(fn=cmd_identity_list)

    o = sub.add_parser("owner").add_subparsers(dest="sub", required=True)
    os_ = o.add_parser("set")
    os_.add_argument("act_id")
    os_.add_argument("marketer")
    os_.add_argument("--since")
    os_.add_argument("--note")
    os_.set_defaults(fn=cmd_owner_set)
    oo = o.add_parser("orphans")
    oo.add_argument("--cycle", type=int)
    oo.set_defaults(fn=cmd_owner_orphans)

    ac = sub.add_parser("accounts")
    ac.add_argument("--problem", action="store_true",
                    help="только кабинеты не в статусе ACTIVE")
    ac.set_defaults(fn=cmd_accounts)

    f = sub.add_parser("fx").add_subparsers(dest="sub", required=True)
    fs = f.add_parser("set")
    fs.add_argument("date")
    fs.add_argument("currency")
    fs.add_argument("rate")
    fs.set_defaults(fn=cmd_fx_set)
    ff = f.add_parser("fill")
    ff.add_argument("currency")
    ff.add_argument("since")
    ff.add_argument("until")
    ff.set_defaults(fn=cmd_fx_fill)

    r = sub.add_parser("report")
    r.add_argument("--cycle", type=int)
    r.add_argument("--last", type=int, help="последние N циклов")
    r.add_argument("--top", type=int, default=6, help="сколько гео показывать")
    r.set_defaults(fn=cmd_report)

    sub.add_parser("check").set_defaults(fn=cmd_check)
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
