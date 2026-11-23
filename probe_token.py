#!/usr/bin/env python3
"""Чей это токен и сколько ему жить. Только чтение, ничего не меняет.

Запускать на сервере:

    /opt/admetrics/.venv/bin/python /opt/admetrics/probe_token.py
    /opt/admetrics/.venv/bin/python /opt/admetrics/probe_token.py "Людмила"

Отвечает на вопрос, который решает всё остальное: мультитокен из расширения —
это токен ПРИЛОЖЕНИЯ META (Ads Manager, Business Suite), привязанный к сессии
браузера и живущий часы, или полноценный OAuth-токен стороннего приложения,
который можно обменять на шестидесятидневный.

Сам токен не печатается никогда — ни целиком, ни куском.
"""
from __future__ import annotations

import re
import sys
from typing import Any

from core import graph, store


# requests кладёт ПОЛНЫЙ URL в текст исключения, а токен мы передаём
# параметром запроса — значит любая сетевая ошибка печатает токен целиком.
# В диагностическом скрипте, который гоняют на сервере и копируют вывод в
# чат, это худший способ слить секрет. Всё, что уходит на печать, проходит
# через redact.
_SECRET = re.compile(r"(EAA[A-Za-z0-9_\-]{10,})|(://[^/@\s]+:[^/@\s]+@)"
                     r"|(access_token=[^&\s]+)|(input_token=[^&\s]+)")


def redact(text: Any) -> str:
    """Убрать из текста токены и пароли прокси. Применять ко всему выводу."""
    return _SECRET.sub("…", str(text))


def ask(session: Any, path: str, token: str, **extra: str) -> Any:
    """GET к Graph с токеном в параметрах. Ошибку возвращаем, а не роняем:
    половина этих ручек на коротких токенах отвечает отказом, и это тоже ответ."""
    params = {"access_token": token, **extra}
    try:
        resp = session.get(f"{graph.BASE_URL}/{graph.DEFAULT_GRAPH_VERSION}/{path}",
                           params=params, timeout=30)
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — диагностика, не прод
        # Только тип ошибки и короткая суть: полный текст содержит URL с токеном.
        return {"error": {"message": f"{type(exc).__name__}: "
                                     f"{redact(exc)[:160]}"}}


def err(payload: Any) -> str:
    if isinstance(payload, dict) and payload.get("error"):
        e = payload["error"]
        return (f"{redact(e.get('message', '?'))} "
                f"(code {e.get('code')}, subcode {e.get('error_subcode')})")
    return ""


def main() -> int:
    needle = (sys.argv[1] if len(sys.argv) > 1 else "").casefold()
    found = [i for i in store.active_identities()
             if not needle or needle in str(i.get("label", "")).casefold()]
    if not found:
        print("Личек не найдено. Без аргумента берутся все живые.")
        return 1

    for ident in found:
        label = ident.get("label") or ident["id"]
        token = ident.get("secret") or ""
        print(f"\n=== {label} ===")
        if not token:
            print("  секрета нет")
            continue
        # build_session отдаёт тройку и сам разбирает контейнер сессии:
        # в базе лежит не голый токен, а снимок браузерной сессии с куками.
        # meta["kind"] как раз и отвечает, что именно нам выдали.
        session, token, meta = graph.build_session(token, ident.get("proxy", ""))
        print(f"  вид секрета: {meta.get('kind')}"
              + (f" · куков {meta.get('cookies')}" if meta.get("cookies") else "")
              + (f" · UA задан" if meta.get("ua") else ""))

        # 1. Чьё приложение. Это главный вопрос: id приложения Meta означает,
        #    что токен снят с сессии браузера и долгим не бывает в принципе.
        app = ask(session, "app", token, fields="id,name,namespace")
        if err(app):
            print("  приложение:  не ответило —", err(app))
        else:
            print(f"  приложение:  {app.get('name', '?')} · id {app.get('id', '?')}")

        # 2. Кто владелец токена.
        me = ask(session, "me", token, fields="id,name")
        print("  владелец:    " + (err(me) or f"{me.get('name', '?')} · {me.get('id', '?')}"))

        # 3. Права. Короткий сессионный токен обычно несёт их десятками —
        #    OAuth-токен ровно те, что запросили.
        perms = ask(session, "me/permissions", token)
        if err(perms):
            print("  права:       не ответило —", err(perms))
        else:
            granted = [p["permission"] for p in (perms.get("data") or [])
                       if p.get("status") == "granted"]
            head = ", ".join(sorted(granted)[:8])
            print(f"  права:       {len(granted)} шт"
                  + (f" — {head}{'…' if len(granted) > 8 else ''}" if granted else ""))

        # 4. Срок. Без секрета приложения Meta обычно отказывает — и сам
        #    отказ информативен: значит приложение не наше.
        dbg = ask(session, "debug_token", token, input_token=token)
        data = (dbg or {}).get("data") or {}
        if err(dbg):
            print("  срок:        не отдан —", err(dbg))
            print("               (ожидаемо: /debug_token требует секрет"
                  " приложения, выпустившего токен)")
        else:
            print(f"  срок:        expires_at={data.get('expires_at')}"
                  f" · data_access_expires_at={data.get('data_access_expires_at')}"
                  f" · valid={data.get('is_valid')} · app_id={data.get('app_id')}")

        print(f"  подключён:   {ident.get('connected_at') or ident.get('created_at') or '—'}")
    print()
    return 0


if __name__ == "__main__":
    store.init_db()
    raise SystemExit(main())
