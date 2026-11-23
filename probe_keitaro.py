#!/usr/bin/env python3
"""Разведка Keitaro: кампании, домены и то, как из них собирается ссылка.

Ничего не меняет — только читает. Нужен, чтобы писать сопоставление по
фактическим полям этого трекера, а не по памяти о том, как устроен Keitaro
вообще: набор полей отличается от версии к версии.

Запуск на сервере:
    cd /opt/admetrics && set -a && . ./.env && set +a && .venv/bin/python probe_keitaro.py
"""
from __future__ import annotations

import json
import os
import sys

import requests

BASE = os.environ.get("KEITARO_URL", "").strip().rstrip("/")
KEY = os.environ.get("KEITARO_KEY", "").strip()
# Замерено на этом трекере: до 10 кампаний за запрос — четверть секунды,
# на 25 он отдаёт заголовки и залипает. Порог лежит между ними, поэтому
# берём по десять и не геройствуем.
PAGE = 10
TIMEOUT = 45


def get(path: str, **params) -> tuple[int, object]:
    """Чтение Admin API. Только GET: POST на этих путях — создание объекта,
    и ключу «только чтение» он отвечает 403, что легко принять за отсутствие
    доступа вообще. Один раз уже приняли."""
    url = f"{BASE}/admin_api/v1/{path.lstrip('/')}"
    try:
        r = requests.get(url, timeout=TIMEOUT, params=params,
                         headers={"Api-Key": KEY})
    except requests.exceptions.Timeout:
        return -1, "timeout"
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)[:300]
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def fetch_all(path: str) -> list[dict]:
    """Постранично, с автоподстройкой размера страницы.

    Этот Keitaro отдаёт две кампании за 0.2 с, а двести — не отдаёт вовсе:
    время растёт быстрее, чем линейно. Поэтому на таймаут не сдаёмся, а
    уменьшаем страницу вдвое и повторяем с того же места. Фиксированный
    размер тут не работает: сколько именно потянет этот трекер, заранее
    не знает никто.
    """
    out: list[dict] = []
    offset, page = 0, PAGE
    while True:
        code, data = get(path, limit=page, offset=offset)
        if code == -1:
            if page > 2:
                page = max(2, page // 2)
                print(f"  … страница великовата, беру по {page}")
                continue
            print(f"  [!] {path}: не отдаёт даже по {page} на offset={offset}")
            break
        if code != 200 or not isinstance(data, list):
            print(f"  [!] {path} на offset={offset}: {code} {str(data)[:160]}")
            break
        out.extend(data)
        print(f"  … {len(out)}", end="\r", flush=True)
        if len(data) < page:
            break
        offset += page
        if offset > 50000:            # предохранитель от бесконечной страницы
            break
    print(" " * 30, end="\r")
    return out


def main() -> int:
    if not BASE or not KEY:
        print("Нет KEITARO_URL или KEITARO_KEY в окружении")
        return 2
    print(f"Трекер: {BASE}, ключ задан ({len(KEY)} символов)\n")

    camps = fetch_all("campaigns")
    print(f"КАМПАНИЙ: {len(camps)}")
    if not camps:
        return 1
    print("\nПОЛЯ КАМПАНИИ")
    for k, v in sorted(camps[0].items()):
        peek = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        print(f"  {k:22} {type(v).__name__:6} {str(peek)[:64]}")

    # Справочник доменов НЕ запрашиваем: в кампании уже лежит готовый
    # `domain` с полным адресом. А /admin_api/v1/domains отдаёт вообще все
    # домены аккаунта — сотни тысяч записей, включая свободные и
    # припаркованные, — и выкачивать их ради поля, которое уже есть, значит
    # вешать разведку на ровном месте. Один раз повесили.
    def link_of(c: dict) -> str:
        dom = str(c.get("domain") or "").strip().rstrip("/")
        alias = str(c.get("alias") or "").strip()
        if not dom:
            return ""
        if "://" not in dom:
            dom = "https://" + dom
        return f"{dom}/{alias}" if alias else dom

    print("\nССЫЛКИ КАМПАНИЙ (первые 15 активных)")
    shown = 0
    for c in camps:
        if c.get("state") != "active":
            continue
        print(f"  id={c.get('id'):<6} {link_of(c)}")
        print(f"         {str(c.get('group') or '')[:24]:24} {str(c.get('name'))[:44]}")
        shown += 1
        if shown >= 15:
            break

    states: dict[str, int] = {}
    for c in camps:
        states[str(c.get("state"))] = states.get(str(c.get("state")), 0) + 1
    print(f"\nПО СОСТОЯНИЮ: {states}")

    # Сколько кампаний делят один домен: если по одной — сопоставлять можно
    # по голому хосту, без разбора пути. Это и решает, насколько сложным
    # получится матчинг.
    import urllib.parse as _up
    per_host: dict[str, int] = {}
    no_domain = 0
    for c in camps:
        dom = str(c.get("domain") or "").strip()
        if not dom:
            no_domain += 1
            continue
        if "://" not in dom:
            dom = "https://" + dom
        host = (_up.urlparse(dom).hostname or "?").lower()
        per_host[host] = per_host.get(host, 0) + 1
    shared = {h: n for h, n in per_host.items() if n > 1}
    print(f"\nДОМЕНОВ В РАБОТЕ: {len(per_host)} на {len(camps)} кампаний")
    print(f"  без домена: {no_domain}")
    print(f"  с несколькими кампаниями: {len(shared)}")
    for h, n in sorted(shared.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {h} — кампаний {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
