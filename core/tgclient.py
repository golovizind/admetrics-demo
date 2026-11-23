"""
Технические аккаунты Telegram: работа с MTProto (Telethon).

Здесь только то, что разговаривает с Telegram: вход, импорт готовых сессий,
разбор ошибок в статусы, и три операции воркера — проверка, синхронизация
каналов, публикация. Хранение — в `tgstore`, расписание — в `tg_worker.py`.

Telethon импортируется лениво: панель и бот должны подниматься и там, где
его не поставили, — тогда не работает только этот раздел.

Сервер стоит в сети, откуда Telegram закрыт, поэтому каждый аккаунт ходит
через свой прокси. Прокси у аккаунта должен быть постоянным: купленные
аккаунты при входе с нового IP часто уходят во «фриз», а ротация адреса
выглядит для Telegram как вход с нового IP каждый раз.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import re
import sqlite3
import struct
import tempfile
import urllib.parse
from typing import Any

from core.graph import parse_proxy

API_ID = int(os.environ.get("TG_API_ID", "0") or 0)
API_HASH = os.environ.get("TG_API_HASH", "").strip()
# Чем представляется клиент. Для импортированных сессий лучше совпадать
# с тем, что было у продавца, но и общие значения работают.
DEVICE = {
    "device_model": os.environ.get("TG_DEVICE_MODEL", "Admetrics Server"),
    "system_version": os.environ.get("TG_SYSTEM_VERSION", "Linux"),
    "app_version": os.environ.get("TG_APP_VERSION", "1.0"),
    "lang_code": os.environ.get("TG_LANG_CODE", "ru"),
    "system_lang_code": os.environ.get("TG_LANG_CODE", "ru"),
}
# Для локальной отладки там, где Telegram открыт. На сервере — только прокси.
ALLOW_DIRECT = os.environ.get("TG_ALLOW_DIRECT", "0") == "1"

# Служебные отправители кодов: «Telegram» и «Verification Codes».
CODE_SENDERS = {777000, 489000}

# Адреса DC — нужны, чтобы перевести сессию Pyrogram (там только номер DC).
DC_IP = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100",
         4: "149.154.167.91", 5: "91.108.56.130"}


class NotConfigured(RuntimeError):
    pass


def configured() -> str:
    """Пустая строка — всё готово, иначе — что не так, словами."""
    if not API_ID or not API_HASH:
        return ("Не заданы TG_API_ID и TG_API_HASH. Получить: my.telegram.org → "
                "API development tools, затем вписать в .env и перезапустить.")
    try:
        import telethon  # noqa: F401
    except ImportError:
        return "Не установлен telethon: pip install -r requirements.txt"
    return ""


# --------------------------------------------------------------------------- #
# Прокси
# --------------------------------------------------------------------------- #
def proxy_for_telethon(url: str) -> dict[str, Any] | None:
    """`socks5://логин:пароль@хост:порт` → словарь прокси Telethon."""
    if not url:
        if ALLOW_DIRECT:
            return None
        raise ValueError("У аккаунта нет прокси. С сервера Telegram закрыт — "
                         "без прокси аккаунт работать не будет.")
    norm, err = parse_proxy(url)
    if err:
        raise ValueError(f"Прокси не разобрал: {err}")
    p = urllib.parse.urlparse(norm)
    scheme = (p.scheme or "").lower().rstrip("h").rstrip("a")
    kind = {"socks5": "socks5", "socks4": "socks4", "http": "http",
            "https": "http"}.get(scheme)
    if not kind:
        raise ValueError(f"Схема прокси «{p.scheme}» для Telegram не подходит")
    return {"proxy_type": kind, "addr": p.hostname, "port": p.port,
            "username": urllib.parse.unquote(p.username or "") or None,
            "password": urllib.parse.unquote(p.password or "") or None,
            "rdns": True}


# --------------------------------------------------------------------------- #
# Клиент и ошибки
# --------------------------------------------------------------------------- #
def make_client(session: str, proxy_url: str) -> Any:
    err = configured()
    if err:
        raise NotConfigured(err)
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or ""), API_ID, API_HASH,
                          proxy=proxy_for_telethon(proxy_url),
                          # Попыток больше, а ждём каждую меньше: на дешёвых
                          # прокси до Telegram примерно каждое второе подключение
                          # виснет, зато удачное потом держится. Пять коротких
                          # попыток дают ~97% успеха вместо ~75% у двух длинных.
                          connection_retries=5, retry_delay=1, timeout=10,
                          auto_reconnect=True, flood_sleep_threshold=30,
                          **DEVICE)


def is_network(exc: BaseException) -> bool:
    """Сетевой сбой — не повод менять статус: аккаунт мог быть жив."""
    return isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError,
                            TimeoutError)) or type(exc).__name__ in {
        "ProxyConnectionError", "ProxyTimeoutError", "ProxyError"}


def classify(exc: BaseException) -> tuple[str | None, str]:
    """Ошибка Telethon → (статус, пояснение). None — статус не трогать."""
    name = type(exc).__name__
    text = str(exc)
    if is_network(exc):
        return None, f"нет связи: {text or name}"
    if "FROZEN" in text.upper() or "Frozen" in name:
        return "frozen", ("Telegram заморозил аккаунт: писать и вступать нельзя. "
                          "Апелляция — через @SpamBot.")
    if name in {"AuthKeyUnregisteredError", "SessionRevokedError",
                "SessionExpiredError", "AuthKeyDuplicatedError",
                "AuthKeyInvalidError", "UnauthorizedError"}:
        return "revoked", ("Сессия больше не действует (вышли из всех "
                           "устройств, сменили пароль или Telegram сбросил ключ). "
                           "Нужен повторный вход.")
    if name in {"UserDeactivatedError", "UserDeactivatedBanError",
                "PhoneNumberBannedError"}:
        return "banned", "Аккаунт удалён или заблокирован Telegram."
    if name == "FloodWaitError":
        return "error", f"Telegram просит подождать {getattr(exc, 'seconds', '?')} с."
    return "error", f"{name}: {text}"[:300]


def me_dict(user: Any) -> dict[str, Any]:
    return {"id": getattr(user, "id", None),
            "username": getattr(user, "username", "") or "",
            "first_name": getattr(user, "first_name", "") or "",
            "phone": getattr(user, "phone", "") or ""}


def run(coro: Any) -> Any:
    """Выполнить корутину из синхронного кода (поток панели)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Вход по номеру
# --------------------------------------------------------------------------- #
async def send_code(phone: str, proxy_url: str) -> dict[str, Any]:
    """Шаг 1. Возвращает состояние входа для шага 2 (хранится шифрованно)."""
    client = make_client("", proxy_url)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return {"phone": phone, "hash": sent.phone_code_hash,
                "session": client.session.save(),
                "via": type(sent.type).__name__.replace("SentCodeType", "")}
    finally:
        await client.disconnect()


async def sign_in_code(state: dict[str, Any], code: str,
                       proxy_url: str) -> dict[str, Any]:
    """Шаг 2. {'ok': True, session, me} или {'password': True, state}."""
    from telethon.errors import SessionPasswordNeededError
    client = make_client(state["session"], proxy_url)
    await client.connect()
    try:
        try:
            user = await client.sign_in(phone=state["phone"],
                                        code=re.sub(r"\D", "", code),
                                        phone_code_hash=state["hash"])
        except SessionPasswordNeededError:
            return {"password": True,
                    "state": {**state, "session": client.session.save()}}
        return {"ok": True, "session": client.session.save(), "me": me_dict(user)}
    finally:
        await client.disconnect()


async def sign_in_password(state: dict[str, Any], password: str,
                           proxy_url: str) -> dict[str, Any]:
    """Шаг 3 — пароль двухфакторки."""
    client = make_client(state["session"], proxy_url)
    await client.connect()
    try:
        user = await client.sign_in(password=password)
        return {"ok": True, "session": client.session.save(), "me": me_dict(user)}
    finally:
        await client.disconnect()


async def verify(session: str, proxy_url: str) -> dict[str, Any]:
    """Проверить готовую сессию: кому она принадлежит."""
    client = make_client(session, proxy_url)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            from telethon.errors import AuthKeyUnregisteredError
            raise AuthKeyUnregisteredError(request=None)
        return me_dict(await client.get_me())
    finally:
        await client.disconnect()


async def diagnose(proxy_url: str) -> str:
    """Почему не подключились: дотянуться через прокси до сервера Telegram
    напрямую, без протокола. Telethon сам отвечает только «failed 2 time(s)»,
    а действия у разных причин разные."""
    try:
        from python_socks.async_.asyncio import Proxy
    except ImportError:
        return ""
    norm, err = parse_proxy(proxy_url) if proxy_url else ("", "нет прокси")
    if err or not norm:
        return f"прокси не разобран: {err}"
    # Схема с h/a — указание requests, python_socks её не знает.
    norm = re.sub(r"^(socks5)h://|^(socks4)a://", lambda m: (m.group(1) or m.group(2)) + "://", norm)
    host, port = DC_IP[2], 443
    # Шаг 1: жив ли сам прокси. Без этого «таймаут» не отличить от
    # «прокси молча не пускает к Telegram» — а действия у них разные.
    pu = urllib.parse.urlparse(norm)
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection(pu.hostname, pu.port), timeout=10)
        w.close()
    except Exception as exc:  # noqa: BLE001
        return (f"до самого прокси {pu.hostname}:{pu.port} не достучаться "
                f"({type(exc).__name__}: {exc or 'таймаут'}) — проверьте адрес "
                "и что прокси пускает подключения с IP сервера")
    try:
        proxy = Proxy.from_url(norm)
        sock = await proxy.connect(dest_host=host, dest_port=port, timeout=12)
        sock.close()
        return ("прокси до Telegram пускает (TCP до 149.154.167.51:443 открыт) — "
                "сбой случайный, повторите")
    except Exception as exc:  # noqa: BLE001
        name, text = type(exc).__name__, str(exc)
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) \
                or "timed out" in text.lower():
            return ("сам прокси отвечает, но до серверов Telegram за 12 с не "
                    "довёл. Так выглядит блокировка Telegram на стороне "
                    "провайдера прокси (или выход через страну, где Telegram "
                    "закрыт). Нужен прокси с разрешённым Telegram")
        if name == "ProxyConnectionError":
            return f"до самого прокси не достучаться: {text}"
        if name == "ProxyError":
            return ("прокси отказал в подключении к серверам Telegram "
                    f"({text}). Многие резидентные прокси Telegram блокируют — "
                    "нужен прокси, у которого Telegram разрешён")
        return f"{name}: {text}"


LOGIN_ERRORS = {
    "PhoneNumberInvalidError": "Номер не распознан Telegram — проверьте формат (+код страны).",
    "PhoneCodeInvalidError": "Код неверный. Введите заново.",
    "PhoneCodeExpiredError": "Код истёк — запросите новый.",
    "PasswordHashInvalidError": "Пароль 2FA неверный.",
    "PhoneNumberUnoccupiedError": "На этот номер аккаунт не зарегистрирован.",
    "PhoneNumberFloodError": "Слишком много попыток входа — Telegram просит подождать.",
    "PhoneNumberBannedError": "Номер заблокирован Telegram.",
    "ApiIdInvalidError": "TG_API_ID / TG_API_HASH не приняты Telegram.",
}


def explain(exc: BaseException) -> str:
    name = type(exc).__name__
    if name in LOGIN_ERRORS:
        return LOGIN_ERRORS[name]
    status, note = classify(exc)
    return note


# --------------------------------------------------------------------------- #
# Импорт готовой сессии
# --------------------------------------------------------------------------- #
def _telethon_string(dc_id: int, ip: str, port: int, key: bytes) -> str:
    packed = ipaddress.ip_address(ip).packed
    blob = struct.pack(f">B{len(packed)}sH256s", dc_id, packed, port, key)
    return "1" + base64.urlsafe_b64encode(blob).decode()


def import_session(raw: str | bytes) -> str:
    """Готовая сессия в любом из ходовых видов → строка Telethon.

    Понимает: строку Telethon, строку Pyrogram (старую и новую), файл
    `.session` Telethon (SQLite). tdata (Telegram Desktop) — нет: для неё
    нужна отдельная конвертация, проще отдать продавцу формат .session.
    """
    if isinstance(raw, bytes):
        if raw[:16].startswith(b"SQLite format 3"):
            return _from_sqlite(raw)
        raw = raw.decode("utf-8", "ignore")
    s = raw.strip().strip('"').strip("'")
    if not s:
        raise ValueError("Пусто")
    if s.startswith("1") and len(s) in (353, 369):
        return s  # уже Telethon
    try:
        data = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Не похоже ни на строку Telethon, ни на Pyrogram") from exc
    # Pyrogram ≥2: >BI?256sQ?  (dc, api_id, test, key, user_id, is_bot)
    # Pyrogram <2: >B?256sI?   (dc, test, key, user_id, is_bot) — и вариант с Q
    for fmt, key_idx in ((">BI?256sQ?", 3), (">B?256sQ?", 2), (">B?256sI?", 2)):
        if len(data) == struct.calcsize(fmt):
            parts = struct.unpack(fmt, data)
            dc = parts[0]
            if dc not in DC_IP:
                break
            return _telethon_string(dc, DC_IP[dc], 443, parts[key_idx])
    raise ValueError("Формат сессии не распознан. Подходят: строка Telethon, "
                     "строка Pyrogram, файл .session Telethon.")


def _from_sqlite(blob: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".session") as tmp:
        tmp.write(blob)
        tmp.flush()
        conn = sqlite3.connect(tmp.name)
        try:
            row = conn.execute("SELECT dc_id, server_address, port, auth_key"
                               " FROM sessions").fetchone()
        except sqlite3.Error as exc:
            raise ValueError("В файле нет таблицы sessions — это не .session "
                             "Telethon (у Pyrogram другой формат файла)") from exc
        finally:
            conn.close()
    if not row or not row[3]:
        raise ValueError("В файле .session нет ключа авторизации")
    return _telethon_string(int(row[0]), row[1], int(row[2]), bytes(row[3]))


# --------------------------------------------------------------------------- #
# Операции воркера
# --------------------------------------------------------------------------- #
SPAM_OK = ("good news", "no limits", "свободен", "нет ограничений",
           "не ограничен")
SPAM_BAD = ("limited", "ограничен", "restricted", "spam")


async def spamcheck(client: Any) -> tuple[str | None, str]:
    """Спросить @SpamBot. (статус, ответ бота). None — ответ не распознан."""
    async with client.conversation("SpamBot", timeout=25) as conv:
        await conv.send_message("/start")
        resp = await conv.get_response()
    text = (resp.raw_text or "").strip()
    low = text.lower()
    if any(m in low for m in SPAM_OK):
        return "ok", text[:300]
    if any(m in low for m in SPAM_BAD):
        return "spamblock", text[:300]
    return None, text[:300]


async def list_admin_chats(client: Any) -> list[dict[str, Any]]:
    """Каналы и группы, где аккаунт — владелец или админ."""
    from telethon import utils
    from telethon.tl.types import Channel, Chat
    out = []
    async for d in client.iter_dialogs():
        ent = d.entity
        if not isinstance(ent, (Channel, Chat)):
            continue
        creator = bool(getattr(ent, "creator", False))
        rights = getattr(ent, "admin_rights", None)
        if not (creator or rights):
            continue
        rd = rights.to_dict() if rights else {}
        rd.pop("_", None)
        broadcast = isinstance(ent, Channel) and bool(ent.broadcast)
        out.append({
            "chat_id": utils.get_peer_id(ent),
            "title": getattr(ent, "title", "") or "",
            "username": getattr(ent, "username", "") or "",
            "kind": "channel" if broadcast else "group",
            "role": "creator" if creator else "admin",
            "can_post": creator or (rd.get("post_messages", False) if broadcast
                                    else True),
            "can_add_admins": creator or rd.get("add_admins", False),
            "rights": rd,
            "participants": getattr(ent, "participants_count", None),
        })
    return out


async def resolve_chat(client: Any, chat_id: str) -> Any:
    """Сущность канала по id. В StringSession кэша сущностей нет, поэтому
    при промахе подтягиваем диалоги и пробуем ещё раз."""
    try:
        return await client.get_input_entity(int(chat_id))
    except (ValueError, KeyError):
        await client.get_dialogs()
        return await client.get_input_entity(int(chat_id))


def post_link(chat_id: str, username: str, msg_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    cid = str(chat_id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{msg_id}"
    return ""


async def publish(client: Any, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Опубликовать пост. Повторов здесь нет намеренно: Telegram сам
    отбрасывает дубль запроса по `random_id`, а наш повтор был бы новым
    запросом — то есть вторым постом."""
    entity = await resolve_chat(client, chat_id)
    kwargs: dict[str, Any] = {
        "parse_mode": "html" if payload.get("html") else None,
        "link_preview": bool(payload.get("preview", True)),
        "silent": bool(payload.get("silent")),
    }
    media = (payload.get("media_url") or "").strip()
    text = payload.get("text") or ""
    if media:
        msg = await client.send_file(entity, media, caption=text, **{
            k: v for k, v in kwargs.items() if k != "link_preview"})
    else:
        msg = await client.send_message(entity, text, **kwargs)
    return {"message_id": msg.id,
            "link": post_link(chat_id, payload.get("username") or "", msg.id)}
