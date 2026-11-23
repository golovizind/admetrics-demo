"""
Технические аккаунты Telegram: хранение.

Отдельный модуль, а не ещё сотня строк в `store.py`: контур самостоятельный,
и в одном файле его легче проверить целиком. Соединение, шифрование и пул
прокси — общие, из `store`.

Что здесь принципиально:

* Сессия Telethon — это ключ авторизации целиком. Наружу (в API панели) она
  не уходит никогда: `list_accounts()` отдаёт только подсказки, расшифровка
  есть лишь в `secrets()` и `worker_accounts()`, которыми пользуются воркер
  и вход.
* Статус — последнее известное состояние, с историей в `tg_status_log`.
  Переход в плохой статус поднимает алерт; повторная запись того же статуса
  алерт не плодит.
* `tg_jobs` — одновременно очередь для воркера и журнал. Действия от имени
  аккаунта панель сама не выполняет: одна сессия, работающая из двух
  процессов сразу, — верный способ получить её отзыв.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from core import store
from core.store import _q, connect, decrypt, encrypt, new_id, now, rows

# Статусы, при которых аккаунт не работает и человеку надо что-то сделать.
BAD = {"spamblock", "frozen", "revoked", "banned"}
# Статусы, с которыми воркер держит клиента подключённым.
LIVE = {"ok", "spamblock", "unknown", "error"}

STATUS_TEXT = {
    "new": "не авторизован",
    "code_sent": "ждёт код",
    "password_needed": "ждёт пароль 2FA",
    "ok": "работает",
    "spamblock": "спамблок",
    "frozen": "заморожен",
    "revoked": "сессия отозвана",
    "banned": "удалён / забанен",
    "error": "ошибка",
    "unknown": "не проверен",
    "removed": "убран",
}


def _hint(url: str) -> str:
    return store._proxy_hint(url) if url else ""


def _dec(blob: Any) -> str:
    if blob is None:
        return ""
    try:
        return decrypt(blob)
    except Exception:  # noqa: BLE001 — битый блоб не должен ронять список
        return ""


# --------------------------------------------------------------------------- #
# Аккаунты
# --------------------------------------------------------------------------- #
def create_account(label: str, phone: str = "", proxy: str = "") -> str:
    aid = new_id()
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO tg_accounts (id, label, phone, proxy_encrypted, status,
                                     created_at, updated_at)
            VALUES (?, ?, ?, ?, 'new', ?, ?)
        """), (aid, label.strip(), normalize_phone(phone),
               encrypt(proxy) if proxy else None, now(), now()))
    log_status(aid, "new", "заведён")
    return aid


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"+{digits}" if digits else ""


def list_accounts() -> list[dict[str, Any]]:
    """Список для панели. Без секретов — только подсказки и счётчики."""
    with connect() as conn:
        accs = rows(conn.execute("""
            SELECT id, label, phone, tg_user_id, username, first_name,
                   proxy_encrypted, twofa_encrypted, session_encrypted,
                   status, status_note, enabled, last_ok_at, last_check_at,
                   last_spamcheck_at, last_sync_at, created_at
              FROM tg_accounts
             WHERE status <> 'removed'
             ORDER BY label, created_at
        """))
        ch = {r["account_id"]: r for r in rows(conn.execute("""
            SELECT account_id, COUNT(*) AS n,
                   SUM(CASE WHEN role = 'creator' THEN 1 ELSE 0 END) AS owned
              FROM tg_channels GROUP BY account_id"""))}
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
            timespec="seconds")
        codes = {r["account_id"]: r["n"] for r in rows(conn.execute(_q("""
            SELECT account_id, COUNT(*) AS n FROM tg_codes
             WHERE received_at >= ? GROUP BY account_id"""), (since,)))}
    out = []
    for a in accs:
        proxy = _dec(a.pop("proxy_encrypted"))
        a["proxy_hint"] = _hint(proxy)
        a["has_2fa"] = a.pop("twofa_encrypted") is not None
        a["has_session"] = a.pop("session_encrypted") is not None
        a["status_text"] = STATUS_TEXT.get(a["status"], a["status"])
        c = ch.get(a["id"]) or {}
        a["channels"] = int(c.get("n") or 0)
        a["channels_owned"] = int(c.get("owned") or 0)
        a["codes_24h"] = int(codes.get(a["id"]) or 0)
        out.append(a)
    return out


def get_account(account_id: str) -> dict[str, Any] | None:
    for a in list_accounts():
        if a["id"] == account_id:
            return a
    return None


def secrets(account_id: str) -> dict[str, str]:
    """Расшифрованные секреты одного аккаунта. Только для входа и воркера."""
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT phone, session_encrypted, proxy_encrypted, twofa_encrypted,
                   login_encrypted, status
              FROM tg_accounts WHERE id = ?"""), (account_id,)))
    if not found:
        return {}
    r = found[0]
    login = _dec(r["login_encrypted"])
    return {"phone": r["phone"] or "", "status": r["status"],
            "session": _dec(r["session_encrypted"]),
            "proxy": _dec(r["proxy_encrypted"]),
            "twofa": _dec(r["twofa_encrypted"]),
            "login": json.loads(login) if login else {}}


def worker_accounts() -> list[dict[str, Any]]:
    """Аккаунты, которые воркер должен держать подключёнными."""
    with connect() as conn:
        ids = [r["id"] for r in rows(conn.execute("""
            SELECT id FROM tg_accounts
             WHERE enabled = 1 AND session_encrypted IS NOT NULL
               AND status IN ('ok', 'spamblock', 'unknown', 'error')"""))]
    out = []
    for aid in ids:
        s = secrets(aid)
        if s.get("session"):
            out.append({"id": aid, **s})
    return out


def set_login_state(account_id: str, state: dict[str, Any] | None,
                    status: str | None = None, note: str = "") -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE tg_accounts SET login_encrypted = ?, updated_at = ?"
                        " WHERE id = ?"),
                     (encrypt(json.dumps(state)) if state else None, now(),
                      account_id))
    if status:
        set_status(account_id, status, note)


def save_session(account_id: str, session: str, me: dict[str, Any],
                 twofa: str = "") -> None:
    """Вход завершён: сохранить сессию и то, кем она оказалась."""
    with connect() as conn:
        conn.execute(_q("""
            UPDATE tg_accounts
               SET session_encrypted = ?, login_encrypted = NULL,
                   tg_user_id = ?, username = ?, first_name = ?,
                   phone = CASE WHEN ? <> '' THEN ? ELSE phone END,
                   updated_at = ?
             WHERE id = ?
        """), (encrypt(session), str(me.get("id") or ""),
               me.get("username") or "", me.get("first_name") or "",
               normalize_phone(me.get("phone") or ""),
               normalize_phone(me.get("phone") or ""), now(), account_id))
        if twofa:
            conn.execute(_q("UPDATE tg_accounts SET twofa_encrypted = ? WHERE id = ?"),
                         (encrypt(twofa), account_id))
    set_status(account_id, "ok", "вход выполнен")


def set_twofa(account_id: str, password: str) -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE tg_accounts SET twofa_encrypted = ? WHERE id = ?"),
                     (encrypt(password) if password else None, account_id))


def set_proxy(account_id: str, proxy: str) -> str:
    """Сменить прокси. Статус сбрасывается: прежний относился к старому пути."""
    with connect() as conn:
        conn.execute(_q("UPDATE tg_accounts SET proxy_encrypted = ?, updated_at = ?"
                        " WHERE id = ?"),
                     (encrypt(proxy) if proxy else None, now(), account_id))
    a = get_account(account_id)
    if a and a["has_session"] and a["status"] not in BAD:
        set_status(account_id, "unknown", "прокси заменён")
    return _hint(proxy)


def set_label(account_id: str, label: str) -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE tg_accounts SET label = ?, updated_at = ? WHERE id = ?"),
                     (label.strip(), now(), account_id))


def set_enabled(account_id: str, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE tg_accounts SET enabled = ?, updated_at = ? WHERE id = ?"),
                     (1 if enabled else 0, now(), account_id))


def remove_account(account_id: str) -> None:
    """Убрать аккаунт: секреты стираются, история и журнал остаются."""
    with connect() as conn:
        conn.execute(_q("""
            UPDATE tg_accounts
               SET session_encrypted = NULL, proxy_encrypted = NULL,
                   twofa_encrypted = NULL, login_encrypted = NULL,
                   enabled = 0, updated_at = ?
             WHERE id = ?"""), (now(), account_id))
        conn.execute(_q("DELETE FROM tg_channels WHERE account_id = ?"), (account_id,))
        conn.execute(_q("UPDATE tg_jobs SET status = 'cancelled', finished_at = ?"
                        " WHERE account_id = ? AND status = 'pending'"),
                     (now(), account_id))
    set_status(account_id, "removed", "убран из панели")


def log_status(account_id: str, status: str, note: str = "") -> None:
    with connect() as conn:
        conn.execute(_q("INSERT INTO tg_status_log (account_id, status, note, at)"
                        " VALUES (?, ?, ?, ?)"), (account_id, status, note[:500], now()))


def set_status(account_id: str, status: str, note: str = "") -> bool:
    """Записать статус. True — если он сменился (тогда же пишется история
    и, для плохих статусов, алерт)."""
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT status, label, username, phone FROM tg_accounts WHERE id = ?"),
            (account_id,)))
        if not found:
            return False
        prev = found[0]["status"]
        stamp = now()
        conn.execute(_q("""
            UPDATE tg_accounts SET status = ?, status_note = ?, updated_at = ?,
                   last_check_at = ?,
                   last_ok_at = CASE WHEN ? = 'ok' THEN ? ELSE last_ok_at END
             WHERE id = ?"""), (status, note[:500], stamp, stamp, status, stamp,
                                account_id))
    if prev == status:
        return False
    log_status(account_id, status, note)
    if status in BAD:
        a = found[0]
        name = a["label"] or (f"@{a['username']}" if a["username"] else a["phone"])
        from core import alerts  # поздний импорт: alerts тянет store
        alerts.raise_alert(
            "tg_account", f"Telegram-аккаунт {name}: {STATUS_TEXT[status]}",
            body=note, severity="bad",
            dedup_key=f"tg:{account_id}:{status}:{stamp}")
    return True


def status_log(account_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT status, note, at FROM tg_status_log WHERE account_id = ?"
            " ORDER BY id DESC LIMIT ?"), (account_id, limit)))


def mark(account_id: str, field: str) -> None:
    assert field in {"last_spamcheck_at", "last_sync_at", "last_check_at"}
    with connect() as conn:
        conn.execute(_q(f"UPDATE tg_accounts SET {field} = ? WHERE id = ?"),
                     (now(), account_id))


# --------------------------------------------------------------------------- #
# Каналы
# --------------------------------------------------------------------------- #
def replace_channels(account_id: str, items: list[dict[str, Any]]) -> int:
    """Переписать список каналов аккаунта целиком, сохранив first_seen_at."""
    stamp = now()
    with connect() as conn:
        first = {r["chat_id"]: r["first_seen_at"] for r in rows(conn.execute(_q(
            "SELECT chat_id, first_seen_at FROM tg_channels WHERE account_id = ?"),
            (account_id,)))}
        conn.execute(_q("DELETE FROM tg_channels WHERE account_id = ?"), (account_id,))
        for it in items:
            cid = str(it["chat_id"])
            conn.execute(_q("""
                INSERT INTO tg_channels (account_id, chat_id, title, username,
                    kind, role, can_post, can_add_admins, rights_json,
                    participants, first_seen_at, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """), (account_id, cid, it.get("title") or "", it.get("username") or "",
                   it.get("kind") or "", it.get("role") or "",
                   1 if it.get("can_post") else 0,
                   1 if it.get("can_add_admins") else 0,
                   json.dumps(it.get("rights") or {}, ensure_ascii=False),
                   it.get("participants"), first.get(cid, stamp), stamp))
    return len(items)


def channels() -> list[dict[str, Any]]:
    """Все каналы, сгруппированные по чату: кто из наших там админ.

    `ours` — сколько наших живых аккаунтов имеют права. Один — значит актив
    держится на единственном аккаунте и уйдёт вместе с ним.
    """
    with connect() as conn:
        data = rows(conn.execute("""
            SELECT c.*, a.label, a.username AS acc_username, a.status AS acc_status
              FROM tg_channels c JOIN tg_accounts a ON a.id = c.account_id
             WHERE a.status <> 'removed'
             ORDER BY c.title
        """))
    by: dict[str, dict[str, Any]] = {}
    for r in data:
        ch = by.setdefault(r["chat_id"], {
            "chat_id": r["chat_id"], "title": r["title"], "username": r["username"],
            "kind": r["kind"], "participants": r["participants"], "admins": []})
        ch["admins"].append({
            "account_id": r["account_id"], "label": r["label"],
            "username": r["acc_username"], "status": r["acc_status"],
            "role": r["role"], "can_post": bool(r["can_post"]),
            "can_add_admins": bool(r["can_add_admins"])})
    out = []
    for ch in by.values():
        alive = [a for a in ch["admins"] if a["status"] not in BAD]
        ch["ours"] = len(alive)
        ch["owner_alive"] = any(a["role"] == "creator" for a in alive)
        ch["owner_known"] = any(a["role"] == "creator" for a in ch["admins"])
        ch["posters"] = [a for a in alive if a["can_post"]]
        ch["at_risk"] = len(alive) <= 1
        out.append(ch)
    out.sort(key=lambda c: (not c["at_risk"], (c["title"] or "").lower()))
    return out


def poster_for(chat_id: str, account_id: str = "") -> str:
    """Каким аккаунтом публиковать в канал: заданным (если он может) или
    первым живым с правом публикации. Пустая строка — некем."""
    for ch in channels():
        if ch["chat_id"] != str(chat_id):
            continue
        ok = [a["account_id"] for a in ch["posters"] if a["status"] == "ok"]
        if account_id:
            return account_id if account_id in ok else ""
        return ok[0] if ok else ""
    return ""


# --------------------------------------------------------------------------- #
# Коды входа
# --------------------------------------------------------------------------- #
CODE_RE = re.compile(r"(?<![\d-])(\d{5,6}|\d{3}-\d{3}|\d{4}(?=\D*$))(?![\d-])")


def extract_code(text: str) -> str:
    m = CODE_RE.search(text or "")
    return m.group(1).replace("-", "") if m else ""


def add_code(account_id: str, sender: str, text: str) -> int:
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO tg_codes (account_id, sender, code, text_encrypted, received_at)
            VALUES (?, ?, ?, ?, ?)"""),
            (account_id, sender[:80], extract_code(text), encrypt(text), now()))
    return 1


def codes(hours: int = 24, account_id: str = "") -> list[dict[str, Any]]:
    """Коды за последние часы. Сам код не отдаётся — только отметка, что он
    есть: показ идёт отдельной ручкой и пишется в журнал."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    sql = ("SELECT c.id, c.account_id, c.sender, c.received_at, c.viewed_at,"
           " c.viewed_by, c.code <> '' AS has_code, a.label, a.username, a.phone"
           " FROM tg_codes c JOIN tg_accounts a ON a.id = c.account_id"
           " WHERE c.received_at >= ?")
    params: list[Any] = [since]
    if account_id:
        sql += " AND c.account_id = ?"
        params.append(account_id)
    sql += " ORDER BY c.id DESC LIMIT 200"
    with connect() as conn:
        return rows(conn.execute(_q(sql), tuple(params)))


def reveal_code(code_id: Any, who: str) -> dict[str, Any] | None:
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT id, account_id, sender, code, text_encrypted, received_at"
            " FROM tg_codes WHERE id = ?"), (code_id,)))
        if not found:
            return None
        conn.execute(_q("UPDATE tg_codes SET viewed_at = ?, viewed_by = ? WHERE id = ?"),
                     (now(), who[:80], code_id))
    r = found[0]
    return {"id": r["id"], "account_id": r["account_id"], "sender": r["sender"],
            "code": r["code"], "text": _dec(r.pop("text_encrypted")),
            "received_at": r["received_at"]}


def purge_codes(hours: int = 24) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    with connect() as conn:
        cur = conn.execute(_q("DELETE FROM tg_codes WHERE received_at < ?"), (since,))
        return cur.rowcount or 0


# --------------------------------------------------------------------------- #
# Очередь действий
# --------------------------------------------------------------------------- #
JOB_KINDS = {"check", "sync", "spamcheck", "post"}


def enqueue(account_id: str, kind: str, payload: dict[str, Any] | None = None,
            created_by: str = "", run_at: str = "") -> int:
    assert kind in JOB_KINDS, kind
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO tg_jobs (account_id, kind, payload_json, status,
                                 created_by, run_at, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?)"""),
            (account_id, kind, json.dumps(payload or {}, ensure_ascii=False),
             created_by[:80], run_at or now(), now()))
        got = rows(conn.execute(_q(
            "SELECT MAX(id) AS id FROM tg_jobs WHERE account_id = ? AND kind = ?"),
            (account_id, kind)))
    return int(got[0]["id"])


def _job_out(r: dict[str, Any]) -> dict[str, Any]:
    r["payload"] = json.loads(r.pop("payload_json") or "{}")
    r["result"] = json.loads(r.pop("result_json") or "{}")
    return r


def get_job(job_id: Any) -> dict[str, Any] | None:
    with connect() as conn:
        found = rows(conn.execute(_q("SELECT * FROM tg_jobs WHERE id = ?"), (job_id,)))
    return _job_out(found[0]) if found else None


def jobs(kind: str = "", limit: int = 50) -> list[dict[str, Any]]:
    sql = ("SELECT j.*, a.label, a.username FROM tg_jobs j"
           " LEFT JOIN tg_accounts a ON a.id = j.account_id")
    params: tuple[Any, ...] = ()
    if kind:
        sql += " WHERE j.kind = ?"
        params = (kind,)
    sql += " ORDER BY j.id DESC LIMIT ?"
    with connect() as conn:
        return [_job_out(r) for r in rows(conn.execute(_q(sql), params + (limit,)))]


def claim_jobs(limit: int = 20) -> list[dict[str, Any]]:
    """Забрать созревшие задачи. Статус ставится сразу: второй проход того же
    цикла (или второй воркер по ошибке) их уже не возьмёт."""
    stamp = now()
    with connect() as conn:
        due = rows(conn.execute(_q(
            "SELECT * FROM tg_jobs WHERE status = 'pending' AND run_at <= ?"
            " ORDER BY run_at, id LIMIT ?"), (stamp, limit)))
        taken = []
        for r in due:
            cur = conn.execute(_q(
                "UPDATE tg_jobs SET status = 'running', started_at = ?"
                " WHERE id = ? AND status = 'pending'"), (stamp, r["id"]))
            if cur.rowcount:
                taken.append(_job_out(r))
    return taken


def finish_job(job_id: Any, status: str, result: dict[str, Any] | None = None,
               error: str = "") -> None:
    assert status in {"done", "failed", "unknown", "cancelled"}
    with connect() as conn:
        conn.execute(_q("""
            UPDATE tg_jobs SET status = ?, result_json = ?, error = ?, finished_at = ?
             WHERE id = ?"""),
            (status, json.dumps(result or {}, ensure_ascii=False, default=str),
             error[:500], now(), job_id))


def cancel_job(job_id: Any) -> bool:
    with connect() as conn:
        cur = conn.execute(_q(
            "UPDATE tg_jobs SET status = 'cancelled', finished_at = ?"
            " WHERE id = ? AND status = 'pending'"), (now(), job_id))
        return bool(cur.rowcount)


def requeue_stale(minutes: int = 15) -> int:
    """Задачи, застрявшие в running после падения воркера.

    Публикацию обратно в очередь НЕ возвращаем: могла уйти. Она получает
    `unknown` — пусть человек посмотрит в канал, а не получит дубль поста.
    """
    edge = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
        timespec="seconds")
    with connect() as conn:
        n = conn.execute(_q(
            "UPDATE tg_jobs SET status = 'unknown', finished_at = ?,"
            " error = 'воркер упал во время выполнения — проверьте канал'"
            " WHERE status = 'running' AND kind = 'post' AND started_at < ?"),
            (now(), edge)).rowcount or 0
        n += conn.execute(_q(
            "UPDATE tg_jobs SET status = 'pending', started_at = NULL"
            " WHERE status = 'running' AND kind <> 'post' AND started_at < ?"),
            (edge,)).rowcount or 0
    return n
