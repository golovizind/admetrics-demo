#!/usr/bin/env python3
"""Тесты раздела Telegram-аккаунтов. Без сети: Telethon подменяется.

    python3 test_tg.py
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import sqlite3
import struct
import sys
import tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp()
os.environ["ADMETRICS_DB"] = os.path.join(TMP, "t.sqlite3")
os.environ.setdefault("ADMETRICS_KEY", "x" * 43 + "=")
os.environ["TG_API_ID"] = "12345"
os.environ["TG_API_HASH"] = "h" * 32
try:
    from cryptography.fernet import Fernet
    os.environ["ADMETRICS_KEY"] = Fernet.generate_key().decode()
except ImportError:
    pass

from core import alerts, store, tgclient, tgstore  # noqa: E402
import tg_panel  # noqa: E402
import tg_worker  # noqa: E402

store.init_db()
ADMIN = {"role": "admin", "name": "Админ", "tg_id": "1"}
MK = {"role": "marketer", "name": "М", "id": "m1"}
PROXY = "socks5://u:p@1.2.3.4:1080"
KEY = bytes(range(256))
passed = 0


def ok(cond, msg):
    global passed
    assert cond, msg
    passed += 1


# --- импорт сессий --------------------------------------------------------- #
tele = tgclient._telethon_string(2, "149.154.167.51", 443, KEY)
ok(len(tele) == 353 and tgclient.import_session(tele) == tele, "строка Telethon как есть")
pyro = base64.urlsafe_b64encode(struct.pack(">BI?256sQ?", 2, 1, False, KEY, 777, False)
                                ).decode().rstrip("=")
ok(tgclient.import_session(pyro) == tele, "Pyrogram → Telethon с тем же ключом")
db = os.path.join(TMP, "a.session")
c = sqlite3.connect(db)
c.execute("CREATE TABLE sessions (dc_id, server_address, port, auth_key, takeout_id)")
c.execute("INSERT INTO sessions VALUES (2, '149.154.167.51', 443, ?, NULL)", (KEY,))
c.commit(); c.close()
ok(tgclient.import_session(open(db, "rb").read()) == tele, ".session → Telethon")
try:
    tgclient.import_session("мусор"); ok(False, "мусор принят")
except ValueError:
    ok(True, "")

# --- прокси ---------------------------------------------------------------- #
p = tgclient.proxy_for_telethon("socks5://1.2.3.4:1080:user:s3cret")
ok(p["addr"] == "1.2.3.4" and p["port"] == 1080 and p["password"] == "s3cret",
   f"формат поставщика: {p}")
try:
    tgclient.proxy_for_telethon(""); ok(False, "без прокси пропустили")
except ValueError:
    ok(True, "")

# --- вход: номер → код → 2FA ----------------------------------------------- #
calls = []
async def f_send(phone, proxy):
    calls.append(("send", phone, proxy)); return {"phone": phone, "hash": "H", "session": "S1", "via": "App"}
async def f_code(state, code, proxy):
    calls.append(("code", code)); return {"password": True, "state": {**state, "session": "S2"}}
async def f_pw(state, pw, proxy):
    if pw != "secret":
        raise type("PasswordHashInvalidError", (Exception,), {})()
    return {"ok": True, "session": tele, "me": {"id": 42, "username": "tech7", "first_name": "T", "phone": "79990001122"}}
tgclient.send_code, tgclient.sign_in_code, tgclient.sign_in_password = f_send, f_code, f_pw

ok("error" in tg_panel.api_add(MK, {"phone": "+7999"}), "маркетологу нельзя")
ok("error" in tg_panel.api_add(ADMIN, {"phone": "+79990001122"}), "без прокси нельзя")
r = tg_panel.api_add(ADMIN, {"phone": "+7 (999) 000-11-22", "proxy": PROXY, "label": "TG-07"})
ok(r.get("step") == "code" and calls[0][1] == "+79990001122", f"код запрошен: {r}")
aid = r["id"]
ok(tgstore.get_account(aid)["status"] == "code_sent", "статус ждёт код")
r = tg_panel.api_login_code(ADMIN, {"id": aid, "code": "12 345"})
ok(r.get("step") == "password", f"просит 2FA: {r}")
ok("error" in tg_panel.api_login_password(ADMIN, {"id": aid, "password": "bad"}), "неверный пароль")
r = tg_panel.api_login_password(ADMIN, {"id": aid, "password": "secret"})
ok(r.get("step") == "done", f"вошли: {r}")
a = tgstore.get_account(aid)
ok(a["status"] == "ok" and a["username"] == "tech7" and a["has_2fa"], f"аккаунт: {a}")
ok(not any(k in a for k in ("session", "session_encrypted", "twofa")), "секреты не наружу")
ok(tgstore.secrets(aid)["session"] == tele and tgstore.secrets(aid)["twofa"] == "secret", "сессия шифрованно в базе")
ok({j["kind"] for j in tgstore.jobs()} == {"sync", "spamcheck"}, "после входа — синхронизация и SpamBot")
ok([w["id"] for w in tgstore.worker_accounts()] == [aid], "воркер видит аккаунт")

# --- импорт дубля ---------------------------------------------------------- #
async def f_verify(session, proxy):
    return {"id": 42, "username": "tech7", "phone": "79990001122"}
tgclient.verify = f_verify
ok("уже заведён" in tg_panel.api_import(ADMIN, {"session": tele, "proxy": PROXY}).get("error", ""), "дубль отбит")

# --- каналы ---------------------------------------------------------------- #
b = tgstore.create_account("TG-08", "+70000000000", PROXY)
tgstore.save_session(b, tele, {"id": 43, "username": "tech8"})
tgstore.replace_channels(aid, [
    {"chat_id": -1001, "title": "Demo channel", "username": "demo_channel", "kind": "channel",
     "role": "creator", "can_post": True, "can_add_admins": True},
    {"chat_id": -1002, "title": "MX", "kind": "channel", "role": "admin", "can_post": False}])
tgstore.replace_channels(b, [{"chat_id": -1001, "title": "Demo channel", "kind": "channel",
                              "role": "admin", "can_post": True}])
ch = {c["chat_id"]: c for c in tgstore.channels()}
ok(not ch["-1001"]["at_risk"] and ch["-1002"]["at_risk"], "риск: один админ")
ok(tgstore.poster_for("-1002") == "", "в MX публиковать некем")
ok(tgstore.poster_for("-1001", b) == b, "явный автор принят")
tgstore.set_status(aid, "banned", "тест")
ch = {c["chat_id"]: c for c in tgstore.channels()}
ok(ch["-1001"]["at_risk"] and not ch["-1001"]["owner_alive"], "владелец умер → риск")
ok(tgstore.poster_for("-1001") == b, "публикует оставшийся")
n_alerts = len([x for x in alerts.recent(50) if x["rule"] == "tg_account"])
tgstore.set_status(aid, "banned", "ещё раз")
ok(len([x for x in alerts.recent(50) if x["rule"] == "tg_account"]) == n_alerts == 1,
   "алерт один на переход")
tgstore.set_status(aid, "ok", "ожил")

# --- публикация: проверки -------------------------------------------------- #
ok("error" in tg_panel.api_post(ADMIN, {"chat_id": "-1001", "text": "x" * 4097}), "длинный текст")
ok("error" in tg_panel.api_post(ADMIN, {"chat_id": "-1001", "text": "x" * 1100,
                                        "media_url": "https://a/b.jpg"}), "длинная подпись")
past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
ok("error" in tg_panel.api_post(ADMIN, {"chat_id": "-1001", "text": "hi", "run_at": past}), "прошлое время")
ok("error" in tg_panel.api_post(ADMIN, {"chat_id": "-1002", "text": "hi"}), "некем")
later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
r = tg_panel.api_post(ADMIN, {"chat_id": "-1001", "text": "later", "run_at": later})
ok(r.get("ok"), "отложенный поставлен")
for j in tgstore.claim_jobs():   # сброс служебных задач
    tgstore.finish_job(j["id"], "done")
ok(all(j["payload"].get("text") != "later" for j in tgstore.claim_jobs()), "отложенный не созрел")
ok(tg_panel.api_job_cancel(ADMIN, {"id": r["job_id"]}).get("ok"), "отмена")

# --- воркер: исполнение задач ---------------------------------------------- #
class Msg:
    id = 77
class FakeClient:
    def __init__(self, exc=None): self.exc = exc
    async def get_input_entity(self, x):
        return x
    async def send_message(self, ent, text, **kw):
        if self.exc: raise self.exc
        return Msg()
class H:
    def __init__(self, exc=None): self.client = FakeClient(exc)

def post_job(text):
    jid = tg_panel.api_post(ADMIN, {"chat_id": "-1001", "text": text, "account_id": aid})["job_id"]
    return next(j for j in tgstore.claim_jobs() if j["id"] == jid)

async def go():
    w = tg_worker.Worker()
    w.holders[aid] = H()
    j = post_job("hello")
    await w.run_job(j)
    done = tgstore.get_job(j["id"])
    ok(done["status"] == "done" and done["result"]["link"] == "https://t.me/demo_channel/77", f"пост: {done}")
    w.holders[aid] = H(ConnectionError("reset"))
    j = post_job("net")
    await w.run_job(j)
    ok(tgstore.get_job(j["id"])["status"] == "unknown", "обрыв → неизвестно")
    ok(tgstore.get_account(aid)["status"] == "ok", "обрыв статус не трогает")
    err = type("ChatWriteForbiddenError", (Exception,), {})("нет прав")
    w.holders[aid] = H(err)
    j = post_job("rpc")
    await w.run_job(j)
    ok(tgstore.get_job(j["id"])["status"] == "failed", "отказ сервера → не вышло")
    w.holders[aid] = H(type("AuthKeyUnregisteredError", (Exception,), {})("x"))
    j = post_job("auth")
    await w.run_job(j)
    ok(tgstore.get_account(aid)["status"] == "revoked" and aid not in w.holders,
       "мёртвая сессия → revoked и отключён")
asyncio.run(go())

# --- зависшая публикация после падения воркера ----------------------------- #
tgstore.set_status(aid, "ok", "")
j = post_job("stuck")
with store.connect() as conn:
    conn.execute(store._q("UPDATE tg_jobs SET started_at = ? WHERE id = ?"),
                 ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), j["id"]))
tgstore.requeue_stale()
ok(tgstore.get_job(j["id"])["status"] == "unknown", "зависший пост не переотправляется")

# --- коды ------------------------------------------------------------------ #
ok(tgstore.extract_code("Login code: 52814. Do not give this code") == "52814", "код из текста")
ok(tgstore.extract_code("Ваш код 123-456") == "123456", "код с дефисом")
tgstore.add_code(aid, "Telegram", "Login code: 52814. Do not give this code")
lst = tgstore.codes()
ok(lst and "text" not in lst[0] and "code" not in lst[0], "в списке нет кода")
ok("error" in tg_panel.api_code_reveal(MK, {"id": lst[0]["id"]}), "маркетологу код не показать")
rv = tg_panel.api_code_reveal(ADMIN, {"id": lst[0]["id"]})["code"]
ok(rv["code"] == "52814", "показ")
ok(tgstore.codes()[0]["viewed_by"] == "Админ", "показ записан")

# --- удаление -------------------------------------------------------------- #
tg_panel.api_remove(ADMIN, {"id": b})
ok(tgstore.secrets(b)["session"] == "" and tgstore.get_account(b) is None, "секреты стёрты")

print(f"OK: {passed} проверок")
