"""
Ручки панели для технических аккаунтов Telegram (раздел «Telegram»).

Вынесены из `webapp.py`, чтобы раздел читался целиком. Подключаются там
двумя словарями — `GET_ROUTES` и `POST_ROUTES`.

Правила раздела:

* только админ. Сессия Telegram — это вход в аккаунт, код входа — тоже;
  маркетологу здесь делать нечего;
* сессии, пароли 2FA и тексты кодов в ответы не попадают. Код показывается
  отдельной ручкой, и каждый показ пишется: кто и когда смотрел;
* действия от имени аккаунта (проверка, синхронизация, публикация) панель
  сама не выполняет — ставит задачу воркеру и отдаёт её id. Вход и импорт —
  исключение: в этот момент сессии у воркера ещё нет, делить её не с кем.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

from core import store, tgclient, tgstore
from core.graph import parse_proxy

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def _who(user: dict) -> str:
    return str(user.get("name") or user.get("tg_id") or "admin")


def _deny(user: dict) -> dict[str, Any] | None:
    if user.get("role") != "admin":
        return {"error": "Раздел доступен только администратору"}
    return None


def _proxy(payload: dict) -> tuple[str, str]:
    """(url, ошибка) из proxy_id пула или нового адреса."""
    raw = str(payload.get("proxy") or "").strip()
    if raw:
        url, err = parse_proxy(raw)
        if err:
            return "", f"Прокси не разобрал: {err}"
        store.add_proxy(url)
        return url, ""
    pid = str(payload.get("proxy_id") or "")
    url = store.get_proxy_url(pid) if pid else ""
    if not url and not tgclient.ALLOW_DIRECT:
        return "", "Выберите прокси: с сервера Telegram закрыт, без прокси аккаунт не заработает."
    return url, ""


def _account(payload: dict) -> tuple[str, dict[str, Any] | None]:
    aid = str(payload.get("id") or payload.get("account_id") or "")
    return aid, tgstore.get_account(aid)


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #
def api_accounts(user: dict, params: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    accs = tgstore.list_accounts()
    return {"accounts": accs, "setup_error": tgclient.configured(),
            "bad": sum(1 for a in accs if a["status"] in tgstore.BAD),
            "ok": sum(1 for a in accs if a["status"] == "ok")}


def api_account(user: dict, params: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(params)
    if not acc:
        return {"error": "Аккаунт не найден"}
    chans = [c for c in tgstore.channels()
             if any(a["account_id"] == aid for a in c["admins"])]
    return {"account": acc, "log": tgstore.status_log(aid),
            "channels": chans, "codes": tgstore.codes(account_id=aid),
            "jobs": [j for j in tgstore.jobs(limit=200) if j["account_id"] == aid][:20],
            "setup_error": tgclient.configured()}


def api_channels(user: dict, params: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    ch = tgstore.channels()
    return {"channels": ch, "at_risk": sum(1 for c in ch if c["at_risk"])}


def api_codes(user: dict, params: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    return {"codes": tgstore.codes()}


def api_posts(user: dict, params: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    return {"posts": tgstore.jobs("post", limit=100)}


def api_job(user: dict, params: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    job = tgstore.get_job(params.get("id"))
    return {"job": job} if job else {"error": "Задача не найдена"}


# --------------------------------------------------------------------------- #
# Заведение и вход
# --------------------------------------------------------------------------- #
def api_add(user: dict, payload: dict) -> dict[str, Any]:
    """Завести аккаунт и сразу запросить код на номер."""
    if (d := _deny(user)):
        return d
    if (err := tgclient.configured()):
        return {"error": err}
    phone = tgstore.normalize_phone(str(payload.get("phone") or ""))
    if len(phone) < 8:
        return {"error": "Введите номер с кодом страны, например +79991234567"}
    proxy, err = _proxy(payload)
    if err:
        return {"error": err}
    label = str(payload.get("label") or "").strip() or phone
    aid = tgstore.create_account(label, phone, proxy)
    res = _send_code(aid, phone, proxy)
    return {**res, "id": aid}


def _with_diagnosis(exc: BaseException, proxy: str) -> str:
    """К сетевой ошибке приложить, что именно не так с прокси."""
    msg = tgclient.explain(exc)
    if tgclient.is_network(exc):
        try:
            why = tgclient.run(tgclient.diagnose(proxy))
        except Exception:  # noqa: BLE001
            why = ""
        if why:
            msg = f"Не подключились к Telegram: {why}."
    return msg


def _send_code(aid: str, phone: str, proxy: str) -> dict[str, Any]:
    try:
        state = tgclient.run(tgclient.send_code(phone, proxy))
    except Exception as exc:  # noqa: BLE001
        msg = _with_diagnosis(exc, proxy)
        tgstore.set_status(aid, "new", f"код не отправлен: {msg}")
        return {"error": msg}
    via = {"App": "в приложение Telegram на другом устройстве",
           "Sms": "по SMS", "Call": "звонком", "FlashCall": "звонком",
           "FragmentSms": "через Fragment"}.get(state.get("via", ""), "")
    tgstore.set_login_state(aid, state, "code_sent", f"код отправлен {via}".strip())
    return {"ok": True, "step": "code",
            "message": f"Код отправлен {via}. Введите его ниже.".replace("  ", " ")}


def api_login_start(user: dict, payload: dict) -> dict[str, Any]:
    """Повторный вход для уже заведённого аккаунта (сессия отозвана и т.п.)."""
    if (d := _deny(user)):
        return d
    if (err := tgclient.configured()):
        return {"error": err}
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    sec = tgstore.secrets(aid)
    phone = tgstore.normalize_phone(str(payload.get("phone") or "")) or sec["phone"]
    if not phone:
        return {"error": "У аккаунта нет номера — введите его"}
    if not sec["proxy"] and not tgclient.ALLOW_DIRECT:
        return {"error": "Сначала задайте аккаунту прокси"}
    return _send_code(aid, phone, sec["proxy"])


def _finish(aid: str, res: dict[str, Any], twofa: str = "") -> dict[str, Any]:
    tgstore.save_session(aid, res["session"], res["me"], twofa=twofa)
    me = res["me"]
    name = me.get("first_name") or ""
    if me.get("username"):
        name += f" (@{me['username']})"
    # Сразу посмотреть каналы и спамблок — не ждать планового цикла.
    for kind in ("sync", "spamcheck"):
        tgstore.enqueue(aid, kind, created_by="вход")
    return {"ok": True, "step": "done",
            "message": f"Готово: вошли как {name.strip()}. Воркер подхватит "
                       "аккаунт в течение нескольких секунд."}


def api_login_code(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    sec = tgstore.secrets(aid)
    if not sec.get("login"):
        return {"error": "Вход не начат или уже завершён — запросите код заново"}
    code = str(payload.get("code") or "").strip()
    if not code:
        return {"error": "Введите код"}
    try:
        res = tgclient.run(tgclient.sign_in_code(sec["login"], code, sec["proxy"]))
    except Exception as exc:  # noqa: BLE001
        return {"error": tgclient.explain(exc)}
    if res.get("password"):
        saved = sec.get("twofa") or ""
        if saved:
            # Пароль уже хранится — пробуем им, чтобы не спрашивать лишний раз.
            try:
                ok = tgclient.run(tgclient.sign_in_password(
                    res["state"], saved, sec["proxy"]))
                return _finish(aid, ok)
            except Exception:  # noqa: BLE001 — сохранённый не подошёл, спросим
                pass
        tgstore.set_login_state(aid, res["state"], "password_needed",
                                "нужен пароль 2FA")
        return {"ok": True, "step": "password",
                "message": "На аккаунте включена двухфакторка — введите пароль."}
    return _finish(aid, res)


def api_login_password(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    sec = tgstore.secrets(aid)
    if not sec.get("login"):
        return {"error": "Вход не начат — запросите код заново"}
    pw = str(payload.get("password") or "")
    if not pw:
        return {"error": "Введите пароль"}
    try:
        res = tgclient.run(tgclient.sign_in_password(sec["login"], pw, sec["proxy"]))
    except Exception as exc:  # noqa: BLE001
        return {"error": tgclient.explain(exc)}
    return _finish(aid, res, twofa=pw if payload.get("remember", True) else "")


def api_import(user: dict, payload: dict) -> dict[str, Any]:
    """Завести аккаунт из готовой сессии (строка или файл .session)."""
    if (d := _deny(user)):
        return d
    if (err := tgclient.configured()):
        return {"error": err}
    raw: str | bytes = str(payload.get("session") or "")
    if payload.get("file_b64"):
        try:
            raw = base64.b64decode(str(payload["file_b64"]))
        except Exception:  # noqa: BLE001
            return {"error": "Файл не прочитался"}
    try:
        session = tgclient.import_session(raw)
    except ValueError as exc:
        return {"error": str(exc)}
    proxy, err = _proxy(payload)
    if err:
        return {"error": err}
    try:
        me = tgclient.run(tgclient.verify(session, proxy))
    except Exception as exc:  # noqa: BLE001
        return {"error": "Сессия не прошла проверку: " + _with_diagnosis(exc, proxy)}
    for a in tgstore.list_accounts():
        if a["tg_user_id"] and a["tg_user_id"] == str(me.get("id")):
            return {"error": f"Этот аккаунт уже заведён: {a['label']}"}
    label = (str(payload.get("label") or "").strip()
             or (f"@{me['username']}" if me.get("username") else me.get("phone") or ""))
    aid = tgstore.create_account(label, me.get("phone") or "", proxy)
    twofa = str(payload.get("twofa") or "")
    return {**_finish(aid, {"session": session, "me": me}, twofa=twofa), "id": aid}


# --------------------------------------------------------------------------- #
# Правка
# --------------------------------------------------------------------------- #
def api_set_proxy(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    proxy, err = _proxy(payload)
    if err:
        return {"error": err}
    hint = tgstore.set_proxy(aid, proxy)
    return {"ok": True, "message": f"Прокси заменён на {hint}. Воркер "
            "переподключит аккаунт. Меняйте адрес как можно реже: новый IP "
            "для Telegram выглядит как вход с нового места."}


def api_rename(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    tgstore.set_label(aid, str(payload.get("label") or ""))
    return {"ok": True}


def api_twofa(user: dict, payload: dict) -> dict[str, Any]:
    """Сохранить пароль 2FA (нужен для повторного входа). Наружу не отдаётся."""
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    tgstore.set_twofa(aid, str(payload.get("password") or ""))
    return {"ok": True, "message": "Сохранено" if payload.get("password") else "Убран"}


def api_enable(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    on = bool(payload.get("enabled"))
    tgstore.set_enabled(aid, on)
    return {"ok": True, "message": "Включён" if on else
            "Выключен: воркер отключит его, коды приходить перестанут"}


def api_remove(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    tgstore.remove_account(aid)
    return {"ok": True, "message": "Аккаунт убран, сессия стёрта. Сама сессия "
            "в Telegram жива — завершите её в настройках аккаунта, если нужно."}


# --------------------------------------------------------------------------- #
# Задачи воркеру
# --------------------------------------------------------------------------- #
def api_task(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    aid, acc = _account(payload)
    if not acc:
        return {"error": "Аккаунт не найден"}
    kind = str(payload.get("kind") or "check")
    if kind not in {"check", "sync", "spamcheck"}:
        return {"error": "Неизвестное действие"}
    jid = tgstore.enqueue(aid, kind, created_by=_who(user))
    return {"ok": True, "job_id": jid}


def api_sync_all(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    n = 0
    for a in tgstore.list_accounts():
        if a["enabled"] and a["has_session"] and a["status"] in tgstore.LIVE:
            tgstore.enqueue(a["id"], "sync", created_by=_who(user))
            n += 1
    return {"ok": True, "message": f"Синхронизация поставлена для {n} аккаунтов"}


def api_post(user: dict, payload: dict) -> dict[str, Any]:
    """Публикация в канал (сразу или к сроку)."""
    if (d := _deny(user)):
        return d
    chat_id = str(payload.get("chat_id") or "")
    text = str(payload.get("text") or "")
    media = str(payload.get("media_url") or "").strip()
    if not chat_id:
        return {"error": "Выберите канал"}
    if not text.strip() and not media:
        return {"error": "Пустой пост"}
    limit = CAPTION_LIMIT if media else TEXT_LIMIT
    if len(text) > limit:
        return {"error": f"Текст длиннее {limit} символов"
                         + (" (подпись к картинке)" if media else "")}
    if media and not media.startswith(("http://", "https://")):
        return {"error": "Картинка — ссылкой http(s)"}
    acc_id = tgstore.poster_for(chat_id, str(payload.get("account_id") or ""))
    if not acc_id:
        return {"error": "Нет живого аккаунта с правом публикации в этот канал. "
                         "Синхронизируйте каналы или проверьте статусы."}
    run_at = ""
    if payload.get("run_at"):
        try:
            t = datetime.fromisoformat(str(payload["run_at"]).replace("Z", "+00:00"))
            if t.tzinfo is None:
                return {"error": "Время без часового пояса"}
        except ValueError:
            return {"error": "Время не разобрал"}
        t = t.astimezone(timezone.utc)
        if t < datetime.now(timezone.utc) - timedelta(minutes=1):
            return {"error": "Время уже прошло"}
        if t > datetime.now(timezone.utc) + timedelta(days=60):
            return {"error": "Дальше 60 дней не планируем"}
        run_at = t.isoformat(timespec="seconds")
    ch = next((c for c in tgstore.channels() if c["chat_id"] == chat_id), {})
    jid = tgstore.enqueue(acc_id, "post", {
        "chat_id": chat_id, "title": ch.get("title", ""),
        "username": ch.get("username", ""), "text": text, "media_url": media,
        "html": bool(payload.get("html")), "silent": bool(payload.get("silent")),
        "preview": bool(payload.get("preview", True)),
    }, created_by=_who(user), run_at=run_at)
    return {"ok": True, "job_id": jid,
            "message": "Запланировано" if run_at else "Отправляю…"}


def api_job_cancel(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    ok = tgstore.cancel_job(payload.get("id"))
    return {"ok": True, "message": "Отменено"} if ok else \
        {"error": "Задача уже выполняется или выполнена"}


def api_code_reveal(user: dict, payload: dict) -> dict[str, Any]:
    if (d := _deny(user)):
        return d
    code = tgstore.reveal_code(payload.get("id"), _who(user))
    return {"code": code} if code else {"error": "Код не найден (коды живут сутки)"}


GET_ROUTES = {
    "/api/tg/accounts": api_accounts,
    "/api/tg/account": api_account,
    "/api/tg/channels": api_channels,
    "/api/tg/codes": api_codes,
    "/api/tg/posts": api_posts,
    "/api/tg/job": api_job,
}

POST_ROUTES = {
    "/api/tg/add": api_add,
    "/api/tg/import": api_import,
    "/api/tg/login/start": api_login_start,
    "/api/tg/login/code": api_login_code,
    "/api/tg/login/password": api_login_password,
    "/api/tg/proxy": api_set_proxy,
    "/api/tg/rename": api_rename,
    "/api/tg/twofa": api_twofa,
    "/api/tg/enable": api_enable,
    "/api/tg/remove": api_remove,
    "/api/tg/task": api_task,
    "/api/tg/sync_all": api_sync_all,
    "/api/tg/post": api_post,
    "/api/tg/job/cancel": api_job_cancel,
    "/api/tg/code/reveal": api_code_reveal,
}
