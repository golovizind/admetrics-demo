#!/usr/bin/env python3
"""Проверить контейнер сессии (мультитокен ФБТула) напрямую. Только чтение.

Контейнер читаем ИЗ ФАЙЛА, а не из аргумента: аргумент виден в списке
процессов, а там токен и куки. Положить контейнер в файл (на сервере):

    umask 077
    cat > /tmp/cont.txt   # вставить base64, Enter, Ctrl-D
    /opt/admetrics/.venv/bin/python /opt/admetrics/probe_container.py /tmp/cont.txt [прокси]
    shred -u /tmp/cont.txt   # затереть после

Смысл: этот токен заведомо рабочий (только что из ФБТула). Если он проходит
/me — значит хорошие токены /me проходят, и со страницы я извлекаю не тот
токен. Если и он не проходит — неверна сама проверка. Токен не печатаем,
только префикс приложения (15 симв.) и длину.
"""
from __future__ import annotations

import sys

from core import graph, platforms


def check(session, host, ver, token, path, extra, label):
    try:
        r = session.get(f"https://{host}/{ver}/{path}",
                        params={"access_token": token, **extra}, timeout=30)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"  {label} @ {host}: сбой ({type(exc).__name__})"); return
    if isinstance(data, dict) and data.get("error"):
        e = data["error"]
        print(f"  {label} @ {host}: отказ — {e.get('message','?')}"
              f" (code {e.get('code')}, subcode {e.get('error_subcode')})")
    else:
        got = (data.get("name") or data.get("id")
               or f"{len(data.get('data', []))} записей") if isinstance(data, dict) else "ответ"
        print(f"  {label} @ {host}: ОК — {got}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Укажите путь к файлу с контейнером."); return 1
    proxy = sys.argv[2] if len(sys.argv) > 2 else ""
    raw = open(sys.argv[1], encoding="utf-8").read().strip()

    # Сервер достаёт Meta ТОЛЬКО через прокси личики — напрямую сети нет.
    # У этого контейнера своего прокси нет, поэтому для проверки токена
    # одалживаем любой рабочий из базы: для read-only /me чужой прокси годится,
    # важно лишь дотянуться до graph.facebook.com.
    if not proxy:
        from core import store
        with store.connect() as conn:
            rr = store.rows(conn.execute(store._q(
                "SELECT label, proxy_encrypted FROM identities"
                " WHERE proxy_encrypted IS NOT NULL")))
        for r in rr:
            try:
                px = store.decrypt(r["proxy_encrypted"])
            except Exception:  # noqa: BLE001
                px = ""
            if px:
                proxy = px
                print(f"(одолжен прокси личики «{r['label']}» для проверки)")
                break

    try:
        cont = graph.decode_session_container(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"контейнер не разобран: {exc}"); return 1
    token = str(cont.get("token") or "")
    print(f"токен из контейнера: {token[:15]}… длина {len(token)}")
    print(f"куков: {len(cont.get('cookies') or [])}, "
          f"прокси: {'да' if proxy else 'нет'}")

    ver = graph.DEFAULT_GRAPH_VERSION
    session, _t, _m = graph.build_session(token, proxy)
    print("\\n-- прямые вызовы токеном из контейнера --")
    for host in ("graph.facebook.com", "adsmanager-graph.facebook.com"):
        check(session, host, ver, token, "me", {"fields": "id,name"}, "me")
        check(session, host, ver, token, "me/adaccounts", {"limit": "1"}, "me/adaccounts")

    print("\\n-- через адаптер сборщика (как в реальном сборе) --")
    try:
        adapter = platforms.adapter_for("fb", raw, proxy)
        who = adapter.whoami()
        print(f"  whoami: ОК — {who.get('name','?')} ({who.get('id','?')})")
    except Exception as exc:  # noqa: BLE001
        print(f"  whoami: {type(exc).__name__}: {str(exc)[:120]}")

    print("\\n-- ПОЛНЫЙ КРУГ: добыть НОВЫЙ токен из этой сессии и проверить --")
    dbg = graph.token_page_debug(raw, proxy)
    if dbg.get("error"):
        print(f"  добыть токен НЕ вышло: {dbg['error']}")
        return 0
    fresh = dbg.get("token") or ""
    print(f"  новый токен со страницы: {fresh[:15]}… длина {len(fresh)}")
    try:
        cont = graph.reinject_token(raw, fresh)
        who = platforms.adapter_for("fb", cont, proxy).whoami()
        print(f"  whoami (НОВЫЙ токен + куки): ОК — "
              f"{who.get('name','?')} ({who.get('id','?')})")
        print("  ВЫВОД: круг замкнут. Из живой сессии добывается "
              "рабочий токен.")
    except Exception as exc:  # noqa: BLE001
        print(f"  whoami(новый): {type(exc).__name__}: {str(exc)[:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
