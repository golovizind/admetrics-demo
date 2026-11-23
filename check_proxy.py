#!/usr/bin/env python3
"""Проверить прокси с сервера, не трогая панель.

    python3 check_proxy.py 'socks5://203.0.113.20:5007:логин:пароль'

Ходит через прокси в graph.facebook.com БЕЗ токена: ответ «нужен токен»
доказывает, что связь есть. Пробует оба режима SOCKS — с разрешением имени
на нашей стороне и на стороне прокси, — и говорит, какой работает. Ничего
не сохраняет и ничего не меняет.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from core.graph import (BASE_URL, DEFAULT_GRAPH_VERSION, explain_network,
                        parse_proxy, remote_dns_variant)

OK, BAD = "  ok  ", " ОШИБКА "


def hide(url: str) -> str:
    """Адрес без учётки — вывод может попасть в чат или в тикет."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://…@{rest.rpartition('@')[2]}"


def run(url: str) -> bool:
    session = requests.Session()
    session.proxies.update({"http": url, "https": url})
    started = time.time()
    try:
        resp = session.get(f"{BASE_URL}/{DEFAULT_GRAPH_VERSION}/me", timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"[{BAD}] {hide(url)} — {explain_network(exc, has_proxy=True)}")
        return False
    ms = int((time.time() - started) * 1000)
    ip = ""
    try:
        ip = session.get("https://api.ipify.org", timeout=8).text.strip()
    except Exception:  # noqa: BLE001
        ip = "(не узнали)"
    print(f"[{OK}] {hide(url)} — Meta ответила {resp.status_code} за {ms} мс, "
          f"наружу выходим с {ip}")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url, err = parse_proxy(sys.argv[1])
    if err:
        print(f"[{BAD}] строку не разобрал: {err}")
        return 2
    print(f"Проверяю {hide(url)}\n")
    first = run(url)
    alt = remote_dns_variant(url)
    if alt:
        second = run(alt)
    else:
        second = False
        print("(варианта с разрешением имени на стороне прокси у этой схемы нет)")
    print()
    if first:
        print("Работает как есть — менять ничего не надо.")
    elif second:
        print("Работает только с разрешением имени на СТОРОНЕ ПРОКСИ.\n"
              "В панели: личика → «Проверить доступ» → кнопка переключения,\n"
              f"или сохраните адрес как {hide(alt)}")
    else:
        print("Не работает ни так, ни так. Значит дело не в DNS: с этой машины\n"
              "до прокси либо не доходит, либо он сам наружу не пускает.\n"
              "Проверьте порт, логин и пароль и то, что IP сервера разрешён\n"
              "в личном кабинете поставщика — резидентные прокси часто\n"
              "пускают только с заранее указанных адресов.")
    return 0 if (first or second) else 1


if __name__ == "__main__":
    raise SystemExit(main())
