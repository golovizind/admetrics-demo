#!/usr/bin/env python3
"""Проверка добычи свежего токена из сессии. Только чтение, база не меняется.

    /opt/admetrics/.venv/bin/python /opt/admetrics/probe_refresh.py
    /opt/admetrics/.venv/bin/python /opt/admetrics/probe_refresh.py людмила

Берёт личики НАПРЯМУЮ, любого статуса: смысл теста в том, что токен-снимок
уже мёртв (needs_reconnect), а сессия-куки могла остаться живой. Если из неё
достаётся рабочий токен — снимок в базе больше не нужен, добываем при сборе.

Токен не печатается: только длина, префикс и работает ли он (короткий /me).
Фильтр по имени сверяется в Python (casefold), чтобы кириллица из аргумента
не зависела от локали ssh.
"""
from __future__ import annotations

import os
import sys

from core import graph, platforms, store


def arg_needle() -> str:
    """Аргумент из ssh мог прийти в неверной локали (C/POSIX): Python тогда
    декодирует байты через surrogateescape, и кириллица не совпадает. Достаём
    исходные UTF-8 байты обратно. При нормальной UTF-8-локали это no-op."""
    if len(sys.argv) < 2:
        return ""
    a = sys.argv[1]
    try:
        a = os.fsencode(a).decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return a.casefold()


def load(needle: str) -> list[dict]:
    with store.connect() as conn:
        raw = store.rows(conn.execute(store._q(
            "SELECT id, label, status, secret_encrypted, proxy_encrypted"
            "  FROM identities")))
    out = []
    for r in raw:
        if not r.get("secret_encrypted"):
            continue
        if needle and needle not in str(r.get("label", "")).casefold():
            continue
        try:
            r["secret"] = store.decrypt(r.pop("secret_encrypted"))
            r["proxy"] = store.decrypt(r.pop("proxy_encrypted"))
        except Exception:  # noqa: BLE001
            r["secret"], r["proxy"] = "", ""
        out.append(r)
    return out


def main() -> int:
    needle = arg_needle()
    ids = load(needle)
    if not ids:
        # Покажем, что вообще есть — чтобы сразу видеть, дело в фильтре или
        # в отсутствии секретов.
        with store.connect() as conn:
            have = store.rows(conn.execute(store._q(
                "SELECT label, status,"
                " CASE WHEN secret_encrypted IS NULL THEN 0 ELSE 1 END AS s"
                "  FROM identities")))
        print(f"Не совпало по фильтру «{needle or '—'}». В базе:")
        for r in have:
            print(f"  · {r['label']}  [{r['status']}]  секрет: "
                  + ("есть" if r["s"] else "нет"))
        return 1

    for ident in ids:
        label = ident.get("label") or ident["id"]
        print(f"\n=== {label}  [{ident.get('status')}] ===")
        raw, proxy = ident.get("secret") or "", ident.get("proxy") or ""

        dbg = graph.token_page_debug(raw, proxy)
        for t in dbg.get("tries", []):
            print(f"  · {t['url']}")
            for a in t.get("attempts", []):
                tail = (f"обрыв {a['cut']}" if a.get("cut")
                        else f"HTTP {a.get('status')}")
                print(f"      попытка {a['try']}: {tail}, "
                      f"прочитано {a.get('read')} б, финал {a.get('final_url')}")
                if a.get("snippet"):
                    print(f"        начало: {a['snippet'][:160]}")
        diag = dbg.get("diag") or {}
        if diag:
            print(f"  как записан токен: {diag.get('anchored_context') or '— подписи accessToken не найдено'}")
            print(f"  EAA-строки на странице: {diag.get('eaa_kinds')}")
        token, err = dbg.get("token", ""), dbg.get("error", "")
        if err:
            print(f"  добыть токен НЕ вышло: {err}")
            continue
        print(f"  токен добыт: {token[:6]}… длина {len(token)}")

        # ПРАВИЛЬНАЯ проверка: токен Ads Manager валиден только со своей
        # сессией. Вкладываем свежий токен обратно в контейнер к тем же
        # кукам и проверяем через адаптер сборщика — ровно как в бою.
        try:
            fresh = graph.reinject_token(raw, token)
            adapter = platforms.adapter_for("fb", fresh, proxy)
            who = adapter.whoami()
            print(f"  whoami (токен+куки, как в сборе): ОК — "
                  f"{who.get('name','?')} ({who.get('id','?')})")
            print("  ВЫВОД: сессия жива, свежий токен рабочий. Обновление из "
                  "сессии можно встраивать в сбор.")
        except Exception as exc:  # noqa: BLE001
            print(f"  whoami: {type(exc).__name__}: {str(exc)[:140]}")

    print()
    return 0


if __name__ == "__main__":
    store.init_db()
    raise SystemExit(main())
