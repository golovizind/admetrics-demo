#!/usr/bin/env python3
"""Синхронизировать кампании Keitaro и сопоставить с ними объявления.

То же, что делает сбор, но отдельно и с подробным отчётом — чтобы увидеть
результат сразу и понять, где сопоставление не сходится.

    cd /opt/admetrics && set -a && . ./.env && set +a && .venv/bin/python match_keitaro.py

Без аргументов только считает и печатает, в базу пишет. С `--dry` не пишет
ничего — полезно, когда хочется просто посмотреть.
"""
from __future__ import annotations

import sys
from typing import Any

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from core import keitaro, store


def main() -> int:
    dry = "--dry" in sys.argv
    store.init_db()
    if not keitaro.configured():
        print("Не заданы KEITARO_URL / KEITARO_KEY в .env")
        return 2

    print("Тяну кампании трекера…")
    try:
        raw = keitaro.fetch_campaigns()
    except keitaro.KeitaroError as exc:
        print(f"Не вышло: {exc}")
        return 1
    rows = [keitaro.campaign_row(c) for c in raw]
    active = sum(1 for r in rows if r["state"] == "active")
    print(f"  кампаний {len(rows)}, активных {active}")

    hosts: dict[str, int] = {}
    for r in rows:
        if r["host"]:
            hosts[r["host"]] = hosts.get(r["host"], 0) + 1
    shared = {h: n for h, n in hosts.items() if n > 1}
    print(f"  доменов {len(hosts)}, с несколькими кампаниями {len(shared)}")
    if not dry:
        store.replace_keitaro_campaigns(rows)

    ads = store.ads_with_links()
    print(f"\nОбъявлений со ссылкой: {len(ads)}")
    if not ads:
        print("  Ссылок нет вовсе. Либо сбор ещё не ходил за объявлениями,")
        print("  либо у всех креативов пустой link_url — так бывает у")
        print("  объявлений из постов: ссылка лежит внутри поста страницы.")
        return 0

    matcher = keitaro.Matcher(rows)

    # Ссылки на саму соцсеть — это не трафик на трекер: объявление ведёт в
    # профиль или в пост. Считать их «несопоставленными» значит портить себе
    # статистику и гоняться за тем, чего не существует.
    SOCIAL = ("facebook.com", "fb.com", "fb.me", "instagram.com", "m.me",
              "wa.me", "t.me")

    why: dict[str, int] = {}
    miss_hosts: dict[str, int] = {}
    # Статусы объявлений по каждому несошедшемуся домену: если объявление
    # давно выключено, отсутствие кампании в трекере — норма, её удалили.
    # Без этого разреза «не сошлось» выглядит поломкой, а это просто архив.
    miss_states: dict[str, dict[str, int]] = {}
    social = 0
    hit = 0
    # «Живое» — то, что может откручиваться прямо сейчас. WITH_ISSUES сюда
    # входит намеренно: объявление с замечаниями всё ещё показывается.
    LIVE = ("ACTIVE", "WITH_ISSUES")
    live_total = live_hit = 0
    # Живые и несопоставленные — единственный список, с которым человеку
    # действительно надо что-то сделать. Остальное архив.
    live_miss: list[dict[str, Any]] = []
    for ad in ads:
        link = ad.get("link") or ""
        host = keitaro.host_of(link)
        if any(host == s or host.endswith("." + s) for s in SOCIAL):
            social += 1
            if not dry:
                store.set_ad_keitaro(ad["ad_id"], ad.get("act_id", ""),
                                     ad.get("campaign_id", ""), link, 0,
                                     "ведёт в соцсеть, не на трекер")
            continue
        found, how = matcher.match(link)
        why[how] = why.get(how, 0) + 1
        is_live = str(ad.get("effective_status") or "").upper() in LIVE
        live_total += 1 if is_live else 0
        if found:
            hit += 1
            live_hit += 1 if is_live else 0
        elif is_live:
            live_miss.append({"ad_id": ad["ad_id"],
                              "act_id": ad.get("act_id", ""),
                              "campaign_id": ad.get("campaign_id", ""),
                              "status": ad.get("effective_status", ""),
                              "host": host})
        else:
            key = host or "(без ссылки)"
            miss_hosts[key] = miss_hosts.get(key, 0) + 1
            st = str(ad.get("effective_status") or "?")
            miss_states.setdefault(key, {})
            miss_states[key][st] = miss_states[key].get(st, 0) + 1
        if not dry:
            store.set_ad_keitaro(ad["ad_id"], ad.get("act_id", ""),
                                 ad.get("campaign_id", ""), link,
                                 (found or {}).get("kt_id", 0), how)

    real = len(ads) - social
    print(f"Ведут в соцсеть (не на трекер): {social}")
    # Главная цифра — покрытие среди ЖИВЫХ объявлений. Общий процент меряет
    # в основном архив: остановленные кампании в трекере удаляют, и их
    # объявления не сойдутся никогда — это норма, а не пробел.
    if live_total:
        print(f"СРЕДИ ЖИВЫХ: {live_hit} из {live_total}"
              f"  ({live_hit * 100 // live_total}%)")
        if live_miss:
            print(f"\nЖИВЫЕ, НЕ СОШЛИСЬ — {len(live_miss)}")
            print("  Если домена нет в трекере, объявление льёт в удалённую")
            print("  кампанию: деньги тратятся, клики никуда не приходят.")
            for r in live_miss:
                print(f"    {r['host']:34} {r['status'].lower():12} "
                      f"ad {r['ad_id']}  кампания FB {r['campaign_id']}")
    print(f"Сопоставлено: {hit} из {real} ссылок на трекер"
          + (f"  ({hit * 100 // real}%)" if real else ""))
    print("\nПО СПОСОБУ")
    for how, n in sorted(why.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5}  {how}")

    if miss_hosts:
        # Ищем домен в названиях кампаний по корню, без зоны: домен могли
        # записать с другой зоной или с опечаткой, и тогда точного совпадения
        # нет, а кампания есть. Отвечать «не нашли» в таком случае — значит
        # прятать от человека то, что он найдёт глазами за секунду.
        print(f"\nНЕ СОШЛИСЬ — {len(miss_hosts)} доменов")
        for host, n in sorted(miss_hosts.items(), key=lambda kv: -kv[1]):
            root = host.split(".")[0].lower()
            near = [c for c in rows
                    if root and root in str(c.get("name", "")).lower()]
            hint = ""
            if near:
                hint = ("  ← похоже на: "
                        + "; ".join(f"id {c['kt_id']} «{c['name'][:48]}»"
                                    for c in near[:3]))
            st = miss_states.get(host, {})
            st_txt = ", ".join(f"{k.lower()} {v}" for k, v in
                               sorted(st.items(), key=lambda kv: -kv[1]))
            print(f"  {n:4}  {host}{hint}")
            if st_txt:
                print(f"        объявления: {st_txt}")
    if dry:
        print("\n(--dry: в базу ничего не записано)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
