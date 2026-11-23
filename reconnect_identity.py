#!/usr/bin/env python3
"""Переподключить личку свежим контейнером. Меняет ТОЛЬКО секрет и прокси.

    reconnect_identity.py <файл-с-контейнером> '<прокси>' '<кусок имени>'

Контейнер читаем из файла (в аргументе виден в списке процессов). Прокси —
рабочий socks5h. Имя — подстрока, сверяем в Python (кириллица из ssh не бьётся).
После замены сразу проверяем whoami через адаптер сборщика.
"""
from __future__ import annotations

import os
import sys

from core import graph, platforms, store


def needle() -> str:
    if len(sys.argv) < 4:
        return ""
    a = sys.argv[3]
    try:
        a = os.fsencode(a).decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return a.casefold()


def main() -> int:
    if len(sys.argv) < 3:
        print("Использование: reconnect_identity.py <файл> '<прокси>' '<имя>'")
        return 1
    raw = open(sys.argv[1], encoding="utf-8").read().strip()
    proxy = sys.argv[2]
    n = needle()

    if not graph.looks_like_container(raw):
        print("Это не контейнер-сессия (нет куков) — так менять не будем.")
        return 1

    ids = [i for i in store.list_identities()
           if not n or n in str(i.get("label", "")).casefold()]
    if len(ids) != 1:
        print(f"Нужна ровно одна личка по фильтру «{n or '—'}», нашлось {len(ids)}:")
        for i in ids:
            print(f"  · {i['label']} [{i['status']}]")
        return 1
    ident = ids[0]
    print(f"Переподключаю: {ident['label']} [{ident['status']}]")

    store.replace_identity_secret(ident["id"], raw, proxy=proxy)
    print("  контейнер и прокси сохранены")

    # Сразу проверяем — так же, как сбор: обновление токена из сессии + whoami.
    fresh, used, err = graph.refresh_container(raw, proxy)
    cont = fresh or raw
    if fresh and used != proxy:
        store.update_identity_token(ident["id"], fresh, used)
        print("  прокси переключён на socks5h и сохранён")
    try:
        who = platforms.adapter_for("fb", cont, used or proxy).whoami()
        print(f"  whoami: ОК — {who.get('name','?')} ({who.get('id','?')})")
        print("  ГОТОВО: личка жива, токен обновляется из сессии сам.")
        store.set_identity_status(ident["id"], "ok", "переподключена")
    except Exception as exc:  # noqa: BLE001
        print(f"  whoami: {type(exc).__name__}: {str(exc)[:140]}")
    return 0


if __name__ == "__main__":
    store.init_db()
    raise SystemExit(main())
