"""
Слой доступа к данным Admetrics.

Отвечает за три вещи, в которых обе исходные заготовки ошибались:

  1. **Атрибуция.** Спенд кладётся без владельца, владелец подтягивается на
     чтении из `ad_account_owner` по дате. Интервалы владения не пересекаются —
     это гарантируется на записи (`set_owner`), иначе JOIN задвоит спенд.
  2. **Валюты.** Пересчёт в рубли идёт на чтении, через плотную `fx_rates`.
     Отсутствующий курс НЕ превращается в ноль — он попадает в `missing_rates`
     и виден в отчёте. Молчаливое занижение хуже явной дырки.
  3. **Потерянный спенд.** Кабинет без владельца не выпадает из отчёта: он
     собирается в псевдо-маркетолога `__unassigned__`, чтобы сумма по витрине
     всегда сходилась с суммой по таблице фактов.

Деньги считаются в Decimal. Для контроля финансов float недопустим: сверка
с картами идёт до копейки, а на суммах за цикл двоичная погрешность вылезает.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

from . import cycles, schema

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PG = bool(DATABASE_URL)
DB_PATH = Path(os.environ.get("ADMETRICS_DB", "admetrics.sqlite3"))

UNASSIGNED = "__unassigned__"
ZERO = Decimal("0")

# Адаптеры и коллектор оперируют Decimal (float для денег недопустим). psycopg
# понимает Decimal сам, а sqlite3 — нет и падает на биндинге. Отдаём строкой:
# у денежных колонок affinity REAL/NUMERIC, SQLite приведёт сам, а точность
# при этом не теряется по пути.
sqlite3.register_adapter(Decimal, str)

if PG:  # pragma: no cover — покрывается на проде, локально идёт SQLite
    from psycopg.rows import dict_row

_pool: Any = None
_pool_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Соединение
# --------------------------------------------------------------------------- #
def _q(sql: str) -> str:
    """SQLite понимает '?', Postgres — '%s'."""
    return sql.replace("?", "%s") if PG else sql


def _get_pool() -> Any:  # pragma: no cover
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool

                def _configure(conn: Any) -> None:
                    """Каждое соединение смотрит в нашу схему.

                    DDL создаёт таблицы с явным префиксом, а вот обычные
                    запросы пишутся без него — их и разрешает search_path.
                    `public` оставлен вторым на случай расширений.
                    """
                    conn.execute(
                        f"SET search_path TO {schema.PG_SCHEMA}, public")
                    conn.commit()

                # prepare_threshold=None выключает серверные prepared statements.
                # Без этого при подключении через пулер Neon (PgBouncer в
                # transaction-режиме) psycopg3 после пятого выполнения запроса
                # начнёт их создавать, а PgBouncer раздаёт соединения по кругу —
                # получаем плавающее «prepared statement already exists».
                # Свой пул у нас есть, так что пулер Neon не нужен; но если URL
                # всё же окажется с -pooler, эта строка спасёт.
                _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4,
                                       kwargs={"row_factory": dict_row,
                                               "prepare_threshold": None},
                                       configure=_configure,
                                       max_idle=240.0, timeout=30.0, open=True)
    return _pool


@contextmanager
def connect() -> Iterator[Any]:
    if PG:  # pragma: no cover
        with _get_pool().connection() as conn:
            yield conn
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(cur: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def marketer_brocard_ids() -> dict[str, str]:
    """{marketer_id: id пользователя MyBroCards}. Берём самый частый непустой
    user_id по платежам маркетолога — на случай редких кривых строк."""
    out: dict[str, str] = {}
    with connect() as conn:
        got = rows(conn.execute(_q(
            "SELECT marketer_id, user_id, COUNT(*) AS n FROM card_payments "
            "WHERE marketer_id IS NOT NULL AND user_id <> '' "
            "GROUP BY marketer_id, user_id ORDER BY n DESC")))
    for r in got:
        out.setdefault(r["marketer_id"], r["user_id"])
    return out


def kv_get(key: str, default: str = "") -> str:
    """Служебное значение по ключу из app_kv. Пусто/нет — вернём default."""
    with connect() as conn:
        got = rows(conn.execute(
            _q("SELECT v FROM app_kv WHERE k = ?"), (key,)))
    return got[0]["v"] if got else default


def kv_set(key: str, value: str) -> None:
    """Записать служебное значение (upsert по ключу)."""
    with connect() as conn:
        conn.execute(_q(
            "INSERT INTO app_kv (k, v, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (k) DO UPDATE SET v = excluded.v, "
            "updated_at = excluded.updated_at"),
            (key, value, now()))


def save_smm_order(r: dict[str, Any]) -> None:
    """Создать/обновить заказ SMM-панели по order_id. Поля статуса (charge,
    start_count, remains, status) переписываются при обновлении, остальное —
    только при создании."""
    stamp = now()
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO smm_orders (order_id, service_id, service_name,
                category, link, quantity, charge, currency, start_count,
                remains, status, marketer_id, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (order_id) DO UPDATE SET
                charge = excluded.charge, currency = excluded.currency,
                start_count = excluded.start_count, remains = excluded.remains,
                status = excluded.status, updated_at = excluded.updated_at
        """), (
            str(r.get("order_id")), str(r.get("service_id") or ""),
            str(r.get("service_name") or ""), str(r.get("category") or ""),
            str(r.get("link") or ""), int(r.get("quantity") or 0),
            r.get("charge") or 0, str(r.get("currency") or ""),
            int(r.get("start_count") or 0), int(r.get("remains") or 0),
            str(r.get("status") or ""), r.get("marketer_id"), stamp, stamp))


def update_smm_status(order_id: str, st: dict[str, Any]) -> None:
    """Переписать поля статуса заказа из ответа панели."""
    with connect() as conn:
        conn.execute(_q("""
            UPDATE smm_orders SET charge = ?, currency = ?, start_count = ?,
                remains = ?, status = ?, updated_at = ? WHERE order_id = ?
        """), (
            st.get("charge") or 0, str(st.get("currency") or ""),
            int(st.get("start_count") or 0), int(st.get("remains") or 0),
            str(st.get("status") or ""), now(), str(order_id)))


def list_smm_orders(limit: int = 300) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM smm_orders ORDER BY created_at DESC LIMIT ?"),
            (limit,)))


def smm_orders_open(limit: int = 100) -> list[str]:
    """id заказов, которые ещё могут измениться — их статус стоит обновлять.
    Завершённые/отменённые больше не трогаем."""
    done = ("Completed", "Canceled", "Cancelled", "Refunded")
    ph = ",".join("?" * len(done))
    with connect() as conn:
        got = rows(conn.execute(_q(
            f"SELECT order_id FROM smm_orders WHERE status NOT IN ({ph}) "
            "ORDER BY updated_at ASC LIMIT ?"), (*done, limit)))
    return [r["order_id"] for r in got]


def init_db() -> None:
    with connect() as conn:
        schema.create_all(conn, PG)
    # Прокси, заведённые до появления владельцев, раздаём по личикам.
    # Идемпотентно: у кого владелец уже есть, того не трогаем.
    try:
        taken = claim_orphan_proxies()
        if taken:
            print(f"Прокси получили владельцев: {taken}")
    except Exception as exc:  # noqa: BLE001 — не повод не стартовать
        print(f"Владельцев прокси раздать не вышло: {exc}")
    # Адреса, записанные в формате поставщика, приводим к читаемому виду.
    # Идемпотентно: то, что уже читается, не трогается.
    try:
        mended = repair_proxy_formats()
        if mended:
            print(f"Прокси приведены к читаемому виду: {mended}")
    except Exception as exc:  # noqa: BLE001
        print(f"Починить формат прокси не вышло: {exc}")


# --------------------------------------------------------------------------- #
# Секреты
# --------------------------------------------------------------------------- #
def _fernet() -> Fernet:
    key = os.environ.get("ADMETRICS_KEY")
    if not key:
        raise RuntimeError(
            "Не задан ADMETRICS_KEY. Сгенерировать: python3 -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt((plaintext or "").encode("utf-8"))


def decrypt(blob: Any) -> str:
    if blob is None:
        return ""
    try:
        return _fernet().decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Секрет не расшифровался — сменился ADMETRICS_KEY?") from exc


# --------------------------------------------------------------------------- #
# Пароли — намеренно другая схема, чем у секретов
# --------------------------------------------------------------------------- #
# Пароль ХЕШИРУЕТСЯ необратимо: исходное значение не нужно, нужна только сверка.
# Мультитокен ШИФРУЕТСЯ обратимо: коллектору нужен живой токен, чтобы ходить
# в Meta. Путать эти две схемы нельзя ни в одну сторону.
def hash_password(password: str) -> str:
    import base64
    import hashlib
    salt = os.urandom(16)
    iters = 200_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return (f"pbkdf2${iters}${base64.b64encode(salt).decode()}"
            f"${base64.b64encode(dk).decode()}")


def verify_password(password: str, stored: str | None) -> bool:
    import base64
    import hashlib
    import hmac
    if not stored:
        return False
    try:
        _algo, iters, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:  # noqa: BLE001 — битый хеш = неверный пароль
        return False


def set_password(marketer_id: str, password: str) -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE marketers SET password_hash = ? WHERE id = ?"),
                     (hash_password(password), marketer_id))


def set_marketer_active(marketer_id: str, active: bool) -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE marketers SET active = ? WHERE id = ?"),
                     (1 if active else 0, marketer_id))


def marketer_tg(marketer_id: str) -> str:
    """tg_id маркетолога по его id — для аватарки. Пусто, если не привязан."""
    if not marketer_id:
        return ""
    with connect() as conn:
        got = rows(conn.execute(
            _q("SELECT tg_id FROM marketers WHERE id = ?"), (marketer_id,)))
    return (got[0].get("tg_id") if got else "") or ""


def list_marketers() -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute("""
            SELECT m.*, COUNT(i.id) AS identities,
                   SUM(CASE WHEN i.status = 'needs_reconnect' THEN 1 ELSE 0 END)
                       AS dead_identities
              FROM marketers m LEFT JOIN identities i ON i.marketer_id = m.id
             GROUP BY m.id ORDER BY m.name
        """))


def has_spend_rows(act_id: str) -> bool:
    """Есть ли по кабинету хоть один суточный факт.

    Признак «кабинет уже засеян историей». Пока фактов нет, брать скользящее
    окно бессмысленно: кабинет, добавленный сегодня, показал бы ноль за
    прошлый месяц — хотя открут там был.
    """
    with connect() as conn:
        return bool(rows(conn.execute(
            _q("SELECT 1 FROM spend_daily WHERE act_id = ? LIMIT 1"), (act_id,))))


def effective_account_owner() -> dict[str, str]:
    """act_id → маркетолог: формальный владелец, а нет — по доступу личики.

    Та же логика, что в атрибуции спенда: кабинет без явного владельца
    относится к тому, чья личика имеет к нему доступ, а не в «без владельца».
    """
    with connect() as conn:
        formal = {r["act_id"]: r["marketer_id"] for r in rows(conn.execute(_q(
            "SELECT act_id, marketer_id FROM ad_account_owner"
            " WHERE until IS NULL AND marketer_id IS NOT NULL")))}
        access = {r["act_id"]: r["marketer_id"] for r in rows(conn.execute(_q(
            """SELECT x.act_id, MIN(i.marketer_id) AS marketer_id
                 FROM ad_account_access x JOIN identities i ON i.id = x.identity_id
                WHERE i.marketer_id IS NOT NULL AND i.marketer_id <> ''
                GROUP BY x.act_id""")))}
    out = dict(access)
    out.update(formal)   # формальный владелец приоритетнее доступа
    return out


def list_accounts(with_owner: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT a.* FROM ad_accounts a ORDER BY a.amount_spent DESC"
    if with_owner:
        sql = """
            SELECT a.*, o.marketer_id, m.name AS owner_name
              FROM ad_accounts a
              LEFT JOIN ad_account_owner o
                     ON o.act_id = a.act_id AND o.until IS NULL
              LEFT JOIN marketers m ON m.id = o.marketer_id
             ORDER BY a.amount_spent DESC
        """
    with connect() as conn:
        return rows(conn.execute(sql))


# --------------------------------------------------------------------------- #
# Деньги
# --------------------------------------------------------------------------- #
def dec(value: Any) -> Decimal:
    """Аккуратное приведение к Decimal. float идёт через str, иначе
    Decimal(0.1) даст 0.1000000000000000055511151231257827."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return ZERO


def minor_to_major(value: Any) -> Decimal:
    """Meta отдаёт денежные поля в минорных единицах (центах)."""
    return dec(value) / Decimal(100)


# --------------------------------------------------------------------------- #
# Справочники: маркетологи, личики
# --------------------------------------------------------------------------- #
def create_marketer(name: str, login: str = "", password_hash: str = "",
                    tg_id: str = "", role: str = "marketer",
                    team: str = "", marketer_id: str | None = None) -> str:
    mid = marketer_id or new_id()
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO marketers (id, name, login, password_hash, tg_id,
                                   role, team, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """), (mid, name, login or None, password_hash,
               tg_id or None, role, team, now()))
    return mid


def add_identity(marketer_id: str, secret: str, proxy: str = "",
                 label: str = "", platform: str = "fb",
                 secret_kind: str = "session", session_name: str = "") -> str:
    """Добавить личку. Именно ДОБАВИТЬ, а не заменить: у маркетолога их много."""
    iid = new_id()
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO identities (id, marketer_id, platform, label, session_name,
                                    secret_encrypted, secret_kind,
                                    proxy_encrypted, status,
                                    connected_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)
        """), (iid, marketer_id, platform, label, session_name,
               encrypt(secret), secret_kind,
               encrypt(proxy) if proxy else None, now(), now()))
    return iid


def set_session_name(identity_id: str, name: str) -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE identities SET session_name = ? WHERE id = ?"),
                     (name.strip()[:120], identity_id))


def set_identity_status(identity_id: str, status: str, message: str = "",
                        error_code: int | None = None,
                        error_subcode: int | None = None,
                        raw: str = "") -> None:
    """Обновить статус личики И записать переход в историю.

    История нужна, чтобы отвечать на «когда умерла» и считать среднее время
    жизни токена — одной перезаписываемой колонкой этого не сделать.

    `raw` — дословный текст Meta. На экране он не нужен: там достаточно
    «доступ отозван или истёк». Но в журнал он идёт обязательно, потому что
    отвечает на вопрос, который иначе остаётся догадкой: истекла сессия,
    сменился пароль или сработала защита. Мы этот текст раньше выбрасывали
    и на разборе «почему токены живут по два дня» остались без улик.
    """
    stamp = now()
    with connect() as conn:
        cur = conn.execute(_q("SELECT status FROM identities WHERE id = ?"),
                           (identity_id,))
        current = rows(cur)
        previous = current[0]["status"] if current else None

        conn.execute(_q("""
            UPDATE identities
               SET status = ?, last_error = ?, last_checked_at = ?,
                   last_ok_at = CASE WHEN ? = 'ok' THEN ? ELSE last_ok_at END
             WHERE id = ?
        """), (status, message, stamp, status, stamp, identity_id))

        if previous != status:
            conn.execute(_q("""
                INSERT INTO identity_status_log
                    (identity_id, status, error_code, error_subcode, message, at)
                VALUES (?, ?, ?, ?, ?, ?)
            """), (identity_id, status, error_code, error_subcode,
                   (f"{message} — {raw}" if raw and raw != message
                    else message)[:1000], stamp))


def identity_last_ok(identity_id: str) -> str:
    """Момент последнего успешного сбора по личике — или пустая строка."""
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT last_ok_at FROM identities WHERE id = ?"), (identity_id,)))
    return (found[0]["last_ok_at"] or "") if found else ""


def note_identity_issue(identity_id: str, message: str) -> None:
    """Записать, ПОЧЕМУ личка осталась непроверенной, не трогая статус.

    Сетевой сбой статус менять не должен: пометить живую личку мёртвой
    из-за таймаута — значит зря дёрнуть человека. Но и молчать нельзя:
    без причины «ещё не проверена» висит вечно и выглядит как забытая,
    хотя система честно пыталась и упёрлась в сеть.
    """
    with connect() as conn:
        conn.execute(_q("UPDATE identities SET last_error = ?,"
                        " last_checked_at = ? WHERE id = ?"),
                     (message[:400], now(), identity_id))


def list_identities(marketer_id: str | None = None) -> list[dict[str, Any]]:
    """Все личики с именем владельца — БЕЗ расшифровки секретов.

    Отдельная функция от `active_identities()` намеренно: список для экрана
    не должен таскать за собой расшифрованные мультитокены.
    """
    sql = """
        SELECT i.id, i.label, i.session_name, i.platform, i.status, i.last_error,
               i.last_ok_at, i.last_checked_at, i.created_at, i.connected_at,
               i.platform_user_id, i.marketer_id, i.proxy_encrypted,
               m.name AS marketer_name
          FROM identities i
          JOIN marketers m ON m.id = i.marketer_id
    """
    params: tuple = ()
    if marketer_id:
        sql += " WHERE i.marketer_id = ?"
        params = (marketer_id,)
    sql += " ORDER BY m.name, i.label, i.created_at"
    with connect() as conn:
        out = rows(conn.execute(_q(sql), params))
    # Сам адрес наружу не отдаём, но показать, ЧЕРЕЗ ЧТО ходит личика,
    # обязаны: без этого «поменять прокси» — выбор вслепую, а половина
    # мёртвых личек мертвы именно из-за прокси.
    for row in out:
        blob = row.pop("proxy_encrypted", None)
        url = ""
        if blob:
            try:
                url = decrypt(blob)
            except Exception:  # noqa: BLE001 — нет ключа: подсказки не будет
                url = ""
        row["proxy_hint"] = _proxy_hint(url) if url else ""
        row["proxy_fp"] = _proxy_fingerprint(url) if url else ""
    return out


def get_identity(identity_id: str) -> dict[str, Any] | None:
    """Личка без расшифровки секрета — для проверки владельца перед действием."""
    with connect() as conn:
        found = rows(conn.execute(store_q_identity(), (identity_id,)))
    return found[0] if found else None


def store_q_identity() -> str:
    return _q("""
        SELECT id, marketer_id, label, session_name, platform, status,
               last_error, created_at
          FROM identities WHERE id = ?
    """)


def update_identity_token(identity_id: str, container: str,
                          proxy: str = "") -> None:
    """Тихо обновить контейнер личики свежим токеном из живой сессии.

    Отдельно от replace_identity_secret: то — ручная замена мультитокена
    человеком (и она двигает connected_at и статус). А это — автоматическое
    обновление токена внутри ТОЙ ЖЕ сессии перед сбором: connected_at не
    трогаем (сессия та же, отсчёт её жизни не сбрасывается), статус не пишем
    (это не переподключение, а рутинное продление). Прокси перезаписываем,
    только если он изменился — например, сбор переключил socks5 на socks5h.
    """
    with connect() as conn:
        conn.execute(_q("UPDATE identities SET secret_encrypted = ?"
                        " WHERE id = ?"), (encrypt(container), identity_id))
        if proxy:
            conn.execute(_q("UPDATE identities SET proxy_encrypted = ?"
                            " WHERE id = ?"), (encrypt(proxy), identity_id))


def replace_identity_secret(identity_id: str, secret: str,
                            proxy: str = "") -> None:
    """Заменить мультитокен, сохранив саму личку.

    Именно замена, а не новая запись рядом. К личке привязаны доступы к
    кабинетам (`ad_account_access`) и вся её история открута: заведи вместо
    неё вторую — аналитика порвётся надвое, а в списке навсегда останется
    мёртвый дубль. Прокси перетирается только если прислали новый: старый
    обычно продолжает работать, и терять его на замене токена незачем.
    """
    with connect() as conn:
        # connected_at обновляем вместе с секретом: отсчёт шестидесяти дней
        # идёт от выпуска токена, а не от заведения личики.
        conn.execute(_q("UPDATE identities SET secret_encrypted = ?,"
                        " connected_at = ? WHERE id = ?"),
                     (encrypt(secret), now(), identity_id))
        if proxy:
            conn.execute(
                _q("UPDATE identities SET proxy_encrypted = ? WHERE id = ?"),
                (encrypt(proxy), identity_id))
    # Через set_identity_status, а не прямым UPDATE: он пишет переход в
    # историю, иначе «когда личку чинили» останется неизвестным.
    set_identity_status(identity_id, "unknown", "")


def set_identity_proxy(identity_id: str, proxy: str) -> str:
    """Поменять прокси личики, не трогая токен.

    Прокси и токен живут разными жизнями: прокси меняют при переезде на
    другой IP, при смене поставщика, при подборе адреса под гео — токен при
    этом тот же самый. Требовать вставить мультитокен заново ради смены
    адреса значит заставлять человека лезть в антик без нужды.

    Пустая строка — снять прокси и ходить напрямую. Это осмысленный выбор,
    а не ошибка ввода, поэтому разрешаем.
    """
    proxy = (proxy or "").strip()
    with connect() as conn:
        conn.execute(_q("UPDATE identities SET proxy_encrypted = ? WHERE id = ?"),
                     (encrypt(proxy) if proxy else None, identity_id))
    # Статус сбрасываем: он относился к связке «токен + прошлый прокси».
    # Оставить прежний — значит утверждать про новый путь то, что мы про
    # него ещё не проверяли.
    set_identity_status(identity_id, "unknown", "")
    return _proxy_hint(proxy) if proxy else ""


def proxy_usage() -> dict[str, int]:
    """Сколько личек ходит через каждый прокси — по отпечатку адреса.

    Нужно там, где прокси удаляют: «этот адрес используют 4 личики» — это
    ответ на вопрос «можно ли его трогать», а список без этого числа
    предлагает удалять наугад.
    """
    with connect() as conn:
        idents = rows(conn.execute(_q(
            "SELECT proxy_encrypted FROM identities"
            " WHERE proxy_encrypted IS NOT NULL")))
    used: dict[str, int] = {}
    for row in idents:
        try:
            url = decrypt(row["proxy_encrypted"])
        except Exception:  # noqa: BLE001
            continue
        if url:
            fp = _proxy_fingerprint(url)
            used[fp] = used.get(fp, 0) + 1
    return used


def list_proxies_full(marketer_id: str | None = None) -> list[dict[str, Any]]:
    """Пул с отпечатком и числом пользователей — для экрана управления прокси.

    От `list_proxies` отличается тем, что предназначен не для выпадающего
    списка, а для раздела, где прокси заводят и удаляют: там нужно знать,
    занят ли адрес.
    """
    sql = ("SELECT p.id, p.label, p.hint, p.fingerprint, p.marketer_id,"
           " p.created_at, m.name AS owner_name"
           "  FROM proxies p LEFT JOIN marketers m ON m.id = p.marketer_id")
    params: tuple[Any, ...] = ()
    if marketer_id is not None:
        sql += " WHERE p.marketer_id = ?"
        params = (marketer_id,)
    sql += " ORDER BY p.created_at DESC"
    with connect() as conn:
        out = rows(conn.execute(_q(sql), params))
    used = proxy_usage()
    for row in out:
        row["in_use"] = used.get(row["fingerprint"] or "", 0)
        row.pop("fingerprint", None)
    return out


def set_proxy_url(proxy_id: str, url: str) -> str:
    """Переписать адрес существующей записи пула, сохранив её название.

    Отпечаток пересчитываем — иначе дубли перестанут находиться по нему.
    """
    url = (url or "").strip()
    hint = _proxy_hint(url)
    with connect() as conn:
        found = rows(conn.execute(
            _q("SELECT label, hint FROM proxies WHERE id = ?"), (proxy_id,)))
        if not found:
            return ""
        label = found[0]["label"] if found[0]["label"] != found[0]["hint"] else hint
        conn.execute(_q("""
            UPDATE proxies SET url_encrypted = ?, hint = ?, label = ?,
                               fingerprint = ? WHERE id = ?
        """), (encrypt(url), hint, label, _proxy_fingerprint(url), proxy_id))
    return hint


def delete_proxy(proxy_id: str, marketer_id: str | None = None) -> str:
    """Убрать прокси из пула. Занятый — не трогаем.

    Личики хранят адрес у себя, а не ссылкой на пул, так что удаление их бы
    не сломало — но убрать из списка адрес, на котором сейчас кто-то
    работает, значит потерять возможность выбрать его снова для соседней
    личики. Поэтому отказываем и говорим, сколько личек его держат.
    """
    sql = "SELECT fingerprint FROM proxies WHERE id = ?"
    params: tuple[Any, ...] = (proxy_id,)
    if marketer_id is not None:
        sql += " AND marketer_id = ?"
        params = (proxy_id, marketer_id)
    with connect() as conn:
        found = rows(conn.execute(_q(sql), params))
    if not found:
        return "Прокси не найден"
    n = proxy_usage().get(found[0]["fingerprint"] or "", 0)
    if n:
        return f"Этот прокси используют личики: {n}. Сначала переведите их на другой."
    with connect() as conn:
        conn.execute(_q("DELETE FROM proxies WHERE id = ?"), (proxy_id,))
    return ""


def rename_proxy(proxy_id: str, label: str,
                 marketer_id: str | None = None) -> bool:
    sql = "SELECT id FROM proxies WHERE id = ?"
    params: tuple[Any, ...] = (proxy_id,)
    if marketer_id is not None:
        sql += " AND marketer_id = ?"
        params = (proxy_id, marketer_id)
    with connect() as conn:
        if not rows(conn.execute(_q(sql), params)):
            return False
        conn.execute(_q("UPDATE proxies SET label = ? WHERE id = ?"),
                     (label.strip()[:80], proxy_id))
    return True


def set_identity_enabled(identity_id: str, enabled: bool) -> None:
    """Выключить личку из сбора, не удаляя её.

    Удалять нельзя: к личке привязана история статусов и записи о том, какие
    кабинеты она видела. Выключение обратимо, удаление — нет.
    """
    set_identity_status(identity_id, "unknown" if enabled else "disabled",
                        "" if enabled else "выключена вручную")


def delete_identity(identity_id: str) -> None:
    """Стереть секрет, оставив саму личку и её историю.

    Нужно, когда мультитокен скомпрометирован: собирать по нему больше нельзя,
    но связь «эта личка видела такие-то кабинеты» терять не хочется.
    """
    with connect() as conn:
        conn.execute(_q("""
            UPDATE identities SET secret_encrypted = NULL, proxy_encrypted = NULL,
                   status = 'revoked', last_error = 'секрет удалён'
             WHERE id = ?
        """), (identity_id,))


def active_identities() -> list[dict[str, Any]]:
    """Личики для сбора, с расшифрованными секретами."""
    with connect() as conn:
        cur = conn.execute(_q("""
            SELECT i.*, m.name AS marketer_name
              FROM identities i
              JOIN marketers m ON m.id = i.marketer_id
             WHERE m.active = 1 AND i.secret_encrypted IS NOT NULL
               AND i.status NOT IN ('disabled', 'revoked')
             ORDER BY m.name, i.label
        """))
        result = []
        for row in rows(cur):
            row["secret"] = decrypt(row.pop("secret_encrypted"))
            row["proxy"] = decrypt(row.pop("proxy_encrypted"))
            result.append(row)
        return result


# --------------------------------------------------------------------------- #
# Владение кабинетом — интервальное, без пересечений
# --------------------------------------------------------------------------- #
def set_owner(act_id: str, marketer_id: str, since: str | date,
              note: str = "") -> None:
    """Назначить владельца кабинета с даты `since`.

    Закрывает предыдущий открытый интервал днём раньше. Непересечение
    интервалов — не косметика: витрина джойнит факты к владению, и два
    пересекающихся интервала на один кабинет удвоят спенд в отчёте.
    """
    since_s = since.isoformat() if isinstance(since, date) else str(since)
    prev_day = (date.fromisoformat(since_s) - timedelta(days=1)).isoformat()
    with connect() as conn:
        # Закрыть всё, что открыто и началось раньше.
        conn.execute(_q("""
            UPDATE ad_account_owner
               SET until = ?
             WHERE act_id = ? AND since < ? AND (until IS NULL OR until >= ?)
        """), (prev_day, act_id, since_s, since_s))
        # Удалить интервалы, начинающиеся в тот же день или позже — их
        # переназначаем. Иначе получим пересечение с новым интервалом.
        conn.execute(_q("DELETE FROM ad_account_owner WHERE act_id = ? AND since >= ?"),
                     (act_id, since_s))
        conn.execute(_q("""
            INSERT INTO ad_account_owner (act_id, marketer_id, since, until, note)
            VALUES (?, ?, ?, NULL, ?)
        """), (act_id, marketer_id, since_s, note))


def owner_at(act_id: str, day: str | date) -> str | None:
    """Кто владел кабинетом в этот день. None — владельца не было."""
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    with connect() as conn:
        cur = conn.execute(_q("""
            SELECT marketer_id FROM ad_account_owner
             WHERE act_id = ? AND since <= ? AND (until IS NULL OR until >= ?)
             ORDER BY since DESC
        """), (act_id, day_s, day_s))
        found = rows(cur)
    return found[0]["marketer_id"] if found else None


def check_owner_overlaps() -> list[dict[str, Any]]:
    """Найти пересекающиеся интервалы владения.

    Это единственный способ, которым витрина может начать врать в бо́льшую
    сторону: пересечение даёт лишнюю строку в JOIN и удваивает спенд.
    `set_owner` такого не создаёт, но данные могут попасть в таблицу и мимо него
    (миграция, ручная правка), поэтому проверка должна быть отдельной и дешёвой.
    """
    with connect() as conn:
        cur = conn.execute(_q("""
            SELECT a.act_id, a.marketer_id AS m1, a.since AS s1, a.until AS u1,
                   b.marketer_id AS m2, b.since AS s2, b.until AS u2
              FROM ad_account_owner a
              JOIN ad_account_owner b
                ON a.act_id = b.act_id AND a.since < b.since
             WHERE a.until IS NULL OR a.until >= b.since
        """))
        return rows(cur)


def owner_intervals(act_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.execute(_q("""
            SELECT * FROM ad_account_owner WHERE act_id = ? ORDER BY since
        """), (act_id,))
        return rows(cur)


# --------------------------------------------------------------------------- #
# Реестр активов
# --------------------------------------------------------------------------- #
def upsert_ad_account(row: dict[str, Any]) -> None:
    stamp = now()
    fields = ("act_id", "platform", "bm_id", "name", "currency", "timezone_name",
              "account_status", "status_label", "disable_reason", "disable_label",
              "amount_spent", "balance", "spend_cap", "daily_spend_limit",
              "funding_type", "funding_last4", "funding_display", "created_time",
              "next_bill_date")
    values = [row.get(f) for f in fields]
    updates = ", ".join(f"{f} = ?" for f in fields[1:])
    with connect() as conn:
        conn.execute(_q(f"""
            INSERT INTO ad_accounts ({', '.join(fields)}, first_seen_at, last_seen_at)
            VALUES ({', '.join('?' * len(fields))}, ?, ?)
            ON CONFLICT (act_id) DO UPDATE SET {updates}, last_seen_at = ?
        """), (*values, stamp, stamp, *values[1:], stamp))


def save_snapshot(act_id: str, snapshot_date: str, data: dict[str, Any]) -> None:
    """Суточный срез состояния кабинета — основа для алертов."""
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO ad_account_snapshots
                (act_id, snapshot_date, account_status, disable_reason,
                 daily_spend_limit, spend_cap, balance, amount_spent,
                 funding_last4, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (act_id, snapshot_date) DO UPDATE SET
                account_status = ?, disable_reason = ?, daily_spend_limit = ?,
                spend_cap = ?, balance = ?, amount_spent = ?,
                funding_last4 = ?, captured_at = ?
        """), (act_id, snapshot_date,
               data.get("account_status"), data.get("disable_reason"),
               data.get("daily_spend_limit"), data.get("spend_cap"),
               data.get("balance"), data.get("amount_spent"),
               data.get("funding_last4", ""), now(),
               data.get("account_status"), data.get("disable_reason"),
               data.get("daily_spend_limit"), data.get("spend_cap"),
               data.get("balance"), data.get("amount_spent"),
               data.get("funding_last4", ""), now()))


def snapshot_diff(act_id: str, day: str, prev_day: str) -> dict[str, tuple]:
    """Что изменилось между двумя срезами. Алерт — это всегда диф."""
    with connect() as conn:
        cur = conn.execute(_q("""
            SELECT * FROM ad_account_snapshots
             WHERE act_id = ? AND snapshot_date IN (?, ?)
             ORDER BY snapshot_date
        """), (act_id, prev_day, day))
        found = {r["snapshot_date"]: r for r in rows(cur)}
    before, after = found.get(prev_day), found.get(day)
    if not before or not after:
        return {}
    watched = ("account_status", "disable_reason", "daily_spend_limit",
               "spend_cap", "funding_last4")
    return {f: (before[f], after[f]) for f in watched if before[f] != after[f]}


# --------------------------------------------------------------------------- #
# Факты
# --------------------------------------------------------------------------- #
def upsert_spend_daily(rows_in: list[dict[str, Any]], platform: str = "fb") -> int:
    """Открут по кабинету за день — АВТОРИТЕТНЫЙ факт.

    Именно отсюда берутся итоги по маркетологу и циклу, потому что субъект
    отчёта — маркетолог. Гео-разбивка (`spend_country_daily`) — детализация
    нижнего уровня, и она НЕ должна участвовать в расчёте итога: разбивка
    Meta не всегда сходится с итогом кабинета (строки с неопределённой
    страной, округление), и тогда испортилось бы главное число.
    """
    stamp = now()
    written = 0
    with connect() as conn:
        for r in rows_in:
            payload = (r["date"], platform, r["act_id"], r.get("currency", ""),
                       r.get("spend", 0), r.get("impressions", 0), r.get("clicks", 0),
                       r.get("leads", 0), r.get("purchases", 0), r.get("revenue", 0),
                       r.get("reach", 0), r.get("link_clicks", 0))
            conn.execute(_q("""
                INSERT INTO spend_daily
                    (date, platform, act_id, currency, spend, impressions,
                     clicks, leads, purchases, revenue, reach, link_clicks,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (date, platform, act_id) DO UPDATE SET
                    currency = ?, spend = ?, impressions = ?, clicks = ?,
                    leads = ?, purchases = ?, revenue = ?, reach = ?,
                    link_clicks = ?, updated_at = ?
            """), (*payload, stamp, *payload[3:], stamp))
            written += 1
    return written


def upsert_spend_country(rows_in: list[dict[str, Any]], platform: str = "fb") -> int:
    """Открут по стране доставки — ДЕТАЛИЗАЦИЯ нижнего уровня иерархии.

    Берётся из `breakdowns=country`, то есть из фактической доставки. Итоги
    по маркетологу из этой таблицы не считаются — см. `upsert_spend_daily`.
    """
    stamp = now()
    written = 0
    with connect() as conn:
        for r in rows_in:
            payload = (r["date"], platform, r["act_id"], r["country"],
                       r.get("currency", ""), r.get("spend", 0),
                       r.get("impressions", 0), r.get("clicks", 0),
                       r.get("leads", 0), r.get("purchases", 0))
            conn.execute(_q("""
                INSERT INTO spend_country_daily
                    (date, platform, act_id, country, currency, spend,
                     impressions, clicks, leads, purchases, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (date, platform, act_id, country) DO UPDATE SET
                    currency = ?, spend = ?, impressions = ?, clicks = ?,
                    leads = ?, purchases = ?, updated_at = ?
            """), (*payload, stamp, *payload[4:], stamp))
            written += 1
    return written


def upsert_campaign(r: dict[str, Any]) -> None:
    """Кампания как состояние. Перезаписывается целиком: нас интересует
    «что сейчас», история открута лежит отдельно в spend_campaign_daily."""
    stamp = now()
    fields = ("campaign_id", "act_id", "name", "status", "effective_status",
              "objective", "buying_type", "daily_budget", "lifetime_budget",
              "budget_remaining", "bid_strategy", "issues_json", "created_time")
    values = [r.get(f) for f in fields]
    updates = ", ".join(f"{f} = ?" for f in fields[1:])
    with connect() as conn:
        conn.execute(_q(f"""
            INSERT INTO campaigns ({', '.join(fields)}, updated_at)
            VALUES ({', '.join('?' * len(fields))}, ?)
            ON CONFLICT (campaign_id) DO UPDATE SET {updates}, updated_at = ?
        """), (*values, stamp, *values[1:], stamp))


def upsert_adset(r: dict[str, Any]) -> None:
    stamp = now()
    # Список полей — единственное место, где запись может молча потерять
    # собранное: колонка в таблице есть, в списке нет, и данные уходят в ноль
    # без единой ошибки. Поэтому здесь перечислено ВСЁ, что отдаёт адаптер.
    fields = ("adset_id", "act_id", "campaign_id", "name", "effective_status",
              "status", "optimization_goal", "daily_budget", "lifetime_budget",
              "age_min", "age_max", "genders", "countries", "platforms",
              "interests", "custom_audiences", "excluded", "devices",
              "locales", "billing_event")
    vals = [r.get(f) for f in fields]
    upd = ", ".join(f"{f}=?" for f in fields[1:])
    with connect() as conn:
        conn.execute(_q(f"""
            INSERT INTO adsets ({', '.join(fields)}, updated_at)
            VALUES ({', '.join('?' * len(fields))}, ?)
            ON CONFLICT (adset_id) DO UPDATE SET {upd}, updated_at=?
        """), (*vals, stamp, *vals[1:], stamp))


def account_adsets(act_id: str, since: str, until: str) -> list[dict[str, Any]]:
    """Адсеты кабинета с аудиторией и спендом.

    Спенд адсета — сумма его объявлений: собственных строк открута на этом
    уровне мы не собираем, а `ad_daily` уже есть.
    """
    with connect() as conn:
        base = rows(conn.execute(_q(
            "SELECT * FROM adsets WHERE act_id = ? ORDER BY name"), (act_id,)))
        spend = {r["adset_id"]: dec(r["s"]) for r in rows(conn.execute(_q("""
            SELECT a.adset_id, COALESCE(SUM(d.spend),0) s
              FROM ads a JOIN ad_daily d ON d.ad_id = a.ad_id
             WHERE a.act_id=? AND d.date>=? AND d.date<=?
             GROUP BY a.adset_id
        """), (act_id, since, until)))}
    for a in base:
        a["spend"] = spend.get(a["adset_id"], ZERO)
    return base


def upsert_ad(r: dict[str, Any]) -> None:
    stamp = now()
    fields = ("ad_id", "act_id", "campaign_id", "adset_id", "name",
              "effective_status", "issues_json", "story_id")
    values = [r.get(f) for f in fields]
    updates = ", ".join(f"{f} = ?" for f in fields[1:])
    with connect() as conn:
        conn.execute(_q(f"""
            INSERT INTO ads ({', '.join(fields)}, updated_at)
            VALUES ({', '.join('?' * len(fields))}, ?)
            ON CONFLICT (ad_id) DO UPDATE SET {updates}, updated_at = ?
        """), (*values, stamp, *values[1:], stamp))


def upsert_ad_creative(ad_id: str, c: dict[str, Any], platform: str = "fb") -> None:
    """Креатив объявления: что видит пользователь и куда его ведут.

    Отдельной таблицей от `ads`: состояние объявления меняется часто (статус,
    реджекты), а креатив — редко, и тащить его в каждый диф статусов незачем.
    """
    stamp = now()
    fields = ("title", "body", "thumb_url", "preview_url", "link_url",
              "permalink", "instagram_url", "cta", "object_type", "creative_id")
    values = [str(c.get(f) or "")[:2000] for f in fields]
    updates = ", ".join(f"{f} = ?" for f in fields)
    with connect() as conn:
        conn.execute(_q(f"""
            INSERT INTO ads_meta (platform, ad_id, {', '.join(fields)}, updated_at)
            VALUES (?, ?, {', '.join('?' * len(fields))}, ?)
            ON CONFLICT (platform, ad_id) DO UPDATE SET {updates}, updated_at = ?
        """), (platform, ad_id, *values, stamp, *values, stamp))


def upsert_spend_ads(rows_in: list[dict[str, Any]], platform: str = "fb") -> int:
    """Суточный открут по объявлению — «сколько съело конкретное крео»."""
    stamp = now()
    written = 0
    with connect() as conn:
        for r in rows_in:
            payload = (r["date"], platform, r["ad_id"], r["act_id"],
                       r.get("campaign_id", ""), "", r.get("currency", ""),
                       r.get("spend", 0), r.get("impressions", 0),
                       r.get("clicks", 0), r.get("leads", 0),
                       r.get("purchases", 0), r.get("link_clicks", 0),
                       r.get("revenue", 0))
            conn.execute(_q("""
                INSERT INTO ad_daily
                    (date, platform, ad_id, act_id, campaign_id, country,
                     currency, spend, impressions, clicks, leads, purchases,
                     link_clicks, revenue, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (date, platform, ad_id) DO UPDATE SET
                    act_id = ?, campaign_id = ?, country = ?, currency = ?,
                    spend = ?, impressions = ?, clicks = ?, leads = ?,
                    purchases = ?, link_clicks = ?, revenue = ?, updated_at = ?
            """), (*payload, stamp, *payload[3:], stamp))
            written += 1
    return written


def upsert_breakdowns(rows_in: list[dict[str, Any]], platform: str = "fb") -> int:
    """Открут в разрезе возраста, пола, плейсмента, устройства, региона."""
    stamp = now()
    written = 0
    with connect() as conn:
        for r in rows_in:
            payload = (r["date"], platform, r["act_id"], r["kind"],
                       str(r.get("value") or "—"), r.get("currency", ""),
                       r.get("spend", 0), r.get("impressions", 0),
                       r.get("clicks", 0))
            conn.execute(_q("""
                INSERT INTO spend_breakdown_daily
                    (date, platform, act_id, kind, value, currency, spend,
                     impressions, clicks, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (date, platform, act_id, kind, value) DO UPDATE SET
                    currency = ?, spend = ?, impressions = ?, clicks = ?,
                    updated_at = ?
            """), (*payload, stamp, *payload[5:], stamp))
            written += 1
    return written


def get_ad_account(act_id: str) -> dict[str, Any] | None:
    """Последнее известное состояние кабинета — база для дифа алертов."""
    with connect() as conn:
        found = rows(conn.execute(
            _q("SELECT * FROM ad_accounts WHERE act_id = ?"), (act_id,)))
    return found[0] if found else None


def identity_accounts(identity_id: str) -> list[dict[str, Any]]:
    """Кабинеты, которые видит эта личка.

    Доступ, а не владение: у агентского кабинета доступ бывает у нескольких
    личек сразу, владелец при этом всегда один.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT a.*, m.name AS owner_name
              FROM ad_account_access x
              JOIN ad_accounts a ON a.act_id = x.act_id
              LEFT JOIN ad_account_owner o
                     ON o.act_id = a.act_id AND o.until IS NULL
              LEFT JOIN marketers m ON m.id = o.marketer_id
             WHERE x.identity_id = ?
             ORDER BY a.account_status, a.amount_spent DESC
        """), (identity_id,)))


def identity_totals(identity_id: str, since: str, until: str) -> dict[str, Any]:
    """Открут всех кабинетов личики за период, с производными метриками.

    Считается по доступу, поэтому шаренный кабинет попадает в сводку каждой
    личики, которая его видит. Для «сколько принесла эта личка» это верно,
    а итог маркетолога по-прежнему считается от владения — иначе шаренный
    кабинет удвоил бы общую сумму.
    """
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT COALESCE(SUM(s.spend), 0)       AS spend,
                   COALESCE(SUM(s.impressions), 0) AS impressions,
                   COALESCE(SUM(s.clicks), 0)      AS clicks,
                   COALESCE(SUM(s.link_clicks), 0) AS link_clicks,
                   -- Охват намеренно не суммируется: аудитории разных дней
                   -- пересекаются, и сумма дала бы число больше реального.
                   0                               AS reach,
                   COALESCE(SUM(s.leads), 0)       AS leads,
                   COALESCE(SUM(s.purchases), 0)   AS purchases,
                   COALESCE(SUM(s.revenue), 0)     AS revenue,
                   COUNT(DISTINCT s.act_id)        AS accounts,
                   COUNT(DISTINCT s.date)          AS days,
                   MAX(s.currency)                 AS currency
              FROM ad_account_access x
              JOIN spend_daily s ON s.act_id = x.act_id
             WHERE x.identity_id = ? AND s.date >= ? AND s.date <= ?
        """), (identity_id, since, until)))
    return _with_derived(dict(found[0]) if found else {})


def _with_derived(t: dict[str, Any]) -> dict[str, Any]:
    """Дописать CTR/CPC/CPM и прочее к сумме фактов.

    Отдельной функцией, потому что считается одинаково для кабинета и для
    личики, а храниться не может: суточные CTR и CPM не складываются.
    """
    spend, shows = dec(t.get("spend")), int(t.get("impressions") or 0)
    clicks, link_clicks = int(t.get("clicks") or 0), int(t.get("link_clicks") or 0)
    reach, leads = int(t.get("reach") or 0), dec(t.get("leads"))
    t["spend"] = spend
    t["ctr"] = (Decimal(clicks) * 100 / shows) if shows else ZERO
    t["cpc"] = (spend / clicks) if clicks else ZERO
    t["cpm"] = (spend * 1000 / shows) if shows else ZERO
    t["cost_per_link_click"] = (spend / link_clicks) if link_clicks else ZERO
    t["frequency"] = (Decimal(shows) / reach) if reach else ZERO
    t["cpl"] = (spend / leads) if leads else ZERO
    t["roas"] = (dec(t.get("revenue")) / spend) if spend else ZERO
    return t


def identity_daily(identity_id: str, since: str, until: str
                   ) -> list[dict[str, Any]]:
    """Открут личики по дням — для динамики на её карточке."""
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT s.date, COALESCE(SUM(s.spend), 0) AS spend,
                   MAX(s.currency) AS currency
              FROM ad_account_access x
              JOIN spend_daily s ON s.act_id = x.act_id
             WHERE x.identity_id = ? AND s.date >= ? AND s.date <= ?
             GROUP BY s.date ORDER BY s.date
        """), (identity_id, since, until)))


def account_totals_map(since: str, until: str) -> dict[str, dict[str, Any]]:
    """Итоги по ВСЕМ кабинетам за период, одним запросом.

    Отдельный `account_totals` на каждый кабинет — это столько же обращений
    к базе, сколько кабинетов. На десятке это незаметно, на шести сотнях
    список открывался бы секундами. Здесь одна группировка на всех.
    """
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT act_id,
                   COALESCE(SUM(spend), 0)       AS spend,
                   COALESCE(SUM(impressions), 0) AS impressions,
                   COALESCE(SUM(clicks), 0)      AS clicks,
                   COALESCE(SUM(link_clicks), 0) AS link_clicks,
                   COALESCE(MAX(reach), 0)       AS reach,
                   COALESCE(SUM(leads), 0)       AS leads,
                   COALESCE(SUM(purchases), 0)   AS purchases,
                   COALESCE(SUM(revenue), 0)     AS revenue,
                   COUNT(*)                      AS days
              FROM spend_daily
             WHERE date >= ? AND date <= ?
             GROUP BY act_id
        """), (since, until)))
    return {r["act_id"]: _with_derived(dict(r)) for r in found}


def identity_totals_map(since: str, until: str) -> dict[str, dict[str, Any]]:
    """То же для личек: одна группировка вместо запроса на каждую."""
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT x.identity_id,
                   COALESCE(SUM(s.spend), 0)       AS spend,
                   COALESCE(SUM(s.impressions), 0) AS impressions,
                   COALESCE(SUM(s.clicks), 0)      AS clicks,
                   COALESCE(SUM(s.link_clicks), 0) AS link_clicks,
                   0                               AS reach,
                   COALESCE(SUM(s.leads), 0)       AS leads,
                   COALESCE(SUM(s.purchases), 0)   AS purchases,
                   COALESCE(SUM(s.revenue), 0)     AS revenue,
                   COUNT(DISTINCT s.act_id)        AS accounts,
                   COUNT(DISTINCT s.date)          AS days,
                   MAX(s.currency)                 AS currency
              FROM ad_account_access x
              JOIN spend_daily s ON s.act_id = x.act_id
             WHERE s.date >= ? AND s.date <= ?
             GROUP BY x.identity_id
        """), (since, until)))
    return {r["identity_id"]: _with_derived(dict(r)) for r in found}


def identity_accounts_map() -> dict[str, list[str]]:
    """Какие кабинеты видит каждая личка. Одним запросом, для списков."""
    out: dict[str, list[str]] = {}
    with connect() as conn:
        for r in rows(conn.execute(
                "SELECT identity_id, act_id FROM ad_account_access")):
            out.setdefault(r["identity_id"], []).append(r["act_id"])
    return out


def campaigns_of(act_id: str, since: str, until: str) -> list[dict[str, Any]]:
    """Кампании кабинета: состояние целиком плюс открут за период.

    Производные метрики (CTR, CPC, цена лида, ROAS) дописываются здесь же —
    хранить их нельзя, суточные не складываются в правильные за неделю.
    """
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT c.campaign_id, c.name, c.objective, c.effective_status,
                   c.status, c.buying_type, c.bid_strategy, c.created_time,
                   c.daily_budget, c.lifetime_budget, c.budget_remaining,
                   c.issues_json,
                   COALESCE(SUM(s.spend), 0)       AS spend,
                   COALESCE(SUM(s.impressions), 0) AS impressions,
                   COALESCE(SUM(s.clicks), 0)      AS clicks,
                   COALESCE(SUM(s.link_clicks), 0) AS link_clicks,
                   COALESCE(SUM(s.leads), 0)       AS leads,
                   COALESCE(SUM(s.purchases), 0)   AS purchases,
                   COALESCE(SUM(s.revenue), 0)     AS revenue,
                   MAX(a.currency)                 AS currency
              FROM campaigns c
              JOIN ad_accounts a ON a.act_id = c.act_id
              LEFT JOIN spend_campaign_daily s
                     ON s.campaign_id = c.campaign_id
                    AND s.date >= ? AND s.date <= ?
             WHERE c.act_id = ?
             GROUP BY c.campaign_id
             ORDER BY spend DESC, c.name
        """), (since, until, act_id)))
    return [_with_derived(dict(r)) for r in found]


def account_access(act_id: str) -> dict[str, Any] | None:
    """Токен-личика, через который виден кабинет — с её статусом.

    В операции пользователя кабинет и личика ходят парой (1:1), поэтому
    здоровье доступа показывается прямо на аккаунте, отдельной сущности
    «личика» в интерфейсе нет.
    """
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT i.id, i.session_name, i.label, i.status, i.last_error,
                   i.last_checked_at, i.marketer_id
              FROM ad_account_access x
              JOIN identities i ON i.id = x.identity_id
             WHERE x.act_id = ?
             ORDER BY i.last_checked_at DESC LIMIT 1
        """), (act_id,)))
    return found[0] if found else None


def access_by_account() -> dict[str, dict[str, Any]]:
    """act_id → его токен-личика со статусом. Одним запросом для списков."""
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT x.act_id, i.id, i.session_name, i.label, i.status,
                   i.last_error, i.marketer_id
              FROM ad_account_access x
              JOIN identities i ON i.id = x.identity_id
        """)))
    return {r["act_id"]: r for r in found}


def account_totals(act_id: str, since: str, until: str) -> dict[str, Any]:
    """Сводка по кабинету за период, уже с производными метриками.

    CTR, CPC, CPM и частота считаются здесь, а не хранятся: суточные значения
    этих метрик нельзя складывать — среднее от средних врёт тем сильнее, чем
    неравномернее открут. Считаем от сумм.
    """
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT COALESCE(SUM(spend), 0)       AS spend,
                   COALESCE(SUM(impressions), 0) AS impressions,
                   COALESCE(SUM(clicks), 0)      AS clicks,
                   COALESCE(SUM(link_clicks), 0) AS link_clicks,
                   COALESCE(MAX(reach), 0)       AS reach,
                   COALESCE(SUM(leads), 0)       AS leads,
                   COALESCE(SUM(purchases), 0)   AS purchases,
                   COALESCE(SUM(revenue), 0)     AS revenue,
                   COUNT(*)                      AS days,
                   MIN(date) AS first_day, MAX(date) AS last_day
              FROM spend_daily
             WHERE act_id = ? AND date >= ? AND date <= ?
        """), (act_id, since, until)))
    return _with_derived(dict(found[0]) if found else {})


def top_ads(act_id: str, since: str, until: str, limit: int = 10,
            campaign_id: str = "") -> list[dict[str, Any]]:
    """Объявления по расходу, вместе с креативом и ссылками.

    `campaign_id` сужает выборку до одной кампании — карточка кампании
    показывает свои крео, а не лучшие по всему кабинету: иначе в ней
    оказались бы объявления, к ней не относящиеся.
    """
    narrow = " AND d.campaign_id = ?" if campaign_id else ""
    extra = (campaign_id,) if campaign_id else ()
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT d.ad_id,
                   COALESCE(SUM(d.spend), 0)       AS spend,
                   COALESCE(SUM(d.impressions), 0) AS impressions,
                   COALESCE(SUM(d.clicks), 0)      AS clicks,
                   COALESCE(SUM(d.link_clicks), 0) AS link_clicks,
                   COALESCE(SUM(d.leads), 0)       AS leads,
                   COALESCE(SUM(d.purchases), 0)   AS purchases,
                   MAX(d.currency)                 AS currency,
                   MAX(a.name)                     AS name,
                   MAX(a.effective_status)         AS effective_status,
                   MAX(m.title) AS title, MAX(m.body) AS body,
                   MAX(m.preview_url) AS preview_url,
                   MAX(m.link_url) AS link_url,
                   MAX(m.permalink) AS permalink,
                   MAX(m.instagram_url) AS instagram_url,
                   MAX(m.thumb_url) AS thumb_url,
                   MAX(m.cta) AS cta, MAX(m.object_type) AS object_type
              FROM ad_daily d
              LEFT JOIN ads a ON a.ad_id = d.ad_id
              LEFT JOIN ads_meta m ON m.ad_id = d.ad_id AND m.platform = d.platform
             WHERE d.act_id = ? AND d.date >= ? AND d.date <= ?""" + narrow + """
             GROUP BY d.ad_id
             ORDER BY spend DESC
             LIMIT ?
        """), (act_id, since, until, *extra, limit)))


def ads_without_spend(act_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Объявления, по которым фактов ещё нет.

    Нужны, чтобы «крео» не выглядели пустыми у кабинета, который пока не
    крутил: сами объявления и их картинки уже известны.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT a.ad_id, a.name, a.effective_status,
                   m.title, m.body, m.preview_url, m.link_url, m.permalink,
                   m.instagram_url, m.thumb_url, m.cta, m.object_type
              FROM ads a
              LEFT JOIN ads_meta m ON m.ad_id = a.ad_id
             WHERE a.act_id = ?
             ORDER BY a.updated_at DESC
             LIMIT ?
        """), (act_id, limit)))


def breakdown_totals(act_id: str, kind: str, since: str, until: str
                     ) -> list[dict[str, Any]]:
    """Разбивка открута по одному измерению за период, по убыванию расхода."""
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT value,
                   COALESCE(SUM(spend), 0)       AS spend,
                   COALESCE(SUM(impressions), 0) AS impressions,
                   COALESCE(SUM(clicks), 0)      AS clicks,
                   MAX(currency)                 AS currency
              FROM spend_breakdown_daily
             WHERE act_id = ? AND kind = ? AND date >= ? AND date <= ?
             GROUP BY value
             ORDER BY spend DESC
        """), (act_id, kind, since, until)))


def account_daily(act_id: str, since: str, until: str) -> list[dict[str, Any]]:
    """Открут кабинета по дням — для динамики."""
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT date, spend, impressions, clicks, link_clicks, leads,
                   purchases, revenue, currency
              FROM spend_daily
             WHERE act_id = ? AND date >= ? AND date <= ?
             ORDER BY date
        """), (act_id, since, until)))


def account_countries(act_id: str, since: str, until: str
                      ) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT country AS value,
                   COALESCE(SUM(spend), 0)       AS spend,
                   COALESCE(SUM(impressions), 0) AS impressions,
                   COALESCE(SUM(clicks), 0)      AS clicks,
                   MAX(currency)                 AS currency
              FROM spend_country_daily
             WHERE act_id = ? AND date >= ? AND date <= ?
             GROUP BY country
             ORDER BY spend DESC
        """), (act_id, since, until)))


def pair_transactions(act_id: str, since: str, until: str,
                      window_hours: int = 48) -> dict[str, Any]:
    """Сопоставить списания FB с платежами карт по сумме и времени.

    Самый сильный из машинных сигналов. FB пишет, что списал с кабинета
    $24 в момент T; MyBroCard пишет, что с карты •1234 в момент ≈T ушло
    ровно $24. Совпадение суммы и времени с обеих сторон — почти отпечаток:
    так карта привязывается к кабинету по фактическому событию, а не по
    last4 и тем более не по подписи, которую маркетолог мог переврать.

    Возвращает, сколько транзакций FB нашли себе пару, и на какие карты
    эти пары легли — карта с наибольшим числом совпадений и есть искомая.
    """
    with connect() as conn:
        txs = rows(conn.execute(_q("""
            SELECT time, amount FROM fb_transactions
             WHERE act_id = ? AND amount > 0
               AND time >= ? AND time <= ?
        """), (act_id, since, until + "T23:59:59")))
        pays = rows(conn.execute(_q("""
            SELECT card_id, card_last4, date, amount, user_email
              FROM card_payments
             WHERE state IN ('settled', 'pending') AND amount > 0
               AND date >= ? AND date <= ?
        """), (since, until + "T23:59:59")))

    # Индекс платежей по сумме — сравнение времени только внутри своей суммы,
    # иначе на тысячах платежей это квадрат.
    from collections import defaultdict
    by_amount: dict[str, list[dict]] = defaultdict(list)
    for p in pays:
        by_amount[f"{dec(p['amount']):.2f}"].append(p)

    hits: dict[str, dict[str, Any]] = {}
    paired = 0
    for tx in txs:
        key = f"{dec(tx['amount']):.2f}"
        tt = _epoch(tx["time"])
        best = None
        for p in by_amount.get(key, []):
            dt = abs(_epoch(p["date"]) - tt)
            if dt <= window_hours * 3600 and (best is None or dt < best[0]):
                best = (dt, p)
        if best:
            paired += 1
            p = best[1]
            h = hits.setdefault(p["card_id"], {
                "card_id": p["card_id"], "last4": p["card_last4"],
                "user_email": p["user_email"], "matches": 0, "amount": ZERO})
            h["matches"] += 1
            h["amount"] += dec(p["amount"])

    ranked = sorted(hits.values(), key=lambda h: -h["matches"])
    return {"fb_transactions": len(txs), "paired": paired,
            "cards": ranked, "best": ranked[0] if ranked else None}


def _epoch(value: Any) -> float:
    """ISO или unix-строка → секунды. Для сравнения времён событий."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.isdigit():
        return float(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
        except ValueError:
            return 0.0


def upsert_recovery(session_name: str, farmer: str, account_name: str,
                    fb_link: str, login: str, secret_blob: dict) -> None:
    """Сохранить данные восстановления по имени сессии.

    `secret_blob` — словарь чувствительных полей (пароли, 2FA, резервная
    почта); шифруется целиком в JSON, наружу отдаётся только по запросу
    владельца/админа.
    """
    import json as _json
    name = (session_name or "").strip()
    if not name:
        return
    stamp = now()
    enc = encrypt(_json.dumps(secret_blob, ensure_ascii=False))
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO account_recovery
                (session_name, farmer, account_name, fb_link, login,
                 secret_encrypted, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_name) DO UPDATE SET
                farmer=?, account_name=?, fb_link=?, login=?,
                secret_encrypted=?, updated_at=?
        """), (name, farmer, account_name, fb_link, login, enc, stamp,
               farmer, account_name, fb_link, login, enc, stamp))


def search_recovery(query: str, limit: int = 40) -> list[dict[str, Any]]:
    """Найти строки картотеки по имени аккаунта, сессии, фармеру или логину.

    Отбор делаем в Python, а не в SQL: `LOWER()` в SQLite умеет только
    латиницу, и «Ариана» против «ариана» там не совпадёт никогда. Картотека
    — несколько тысяч строк, читать её целиком дешевле, чем городить
    ICU-коллации ради регистра.

    БЕЗ секретов: список — это поиск, а не показ. Пароли отдаёт `get_recovery`
    по конкретной строке, и только когда её открыли намеренно.
    """
    needle = (query or "").strip().casefold()
    if not needle:
        return []
    with connect() as conn:
        all_rows = rows(conn.execute(_q("""
            SELECT session_name, farmer, account_name, fb_link, login, updated_at
              FROM account_recovery
        """)))
    found = []
    for row in all_rows:
        hay = " ".join(str(row.get(k) or "") for k in
                       ("account_name", "session_name", "farmer", "login"))
        if needle in hay.casefold():
            found.append(row)
            if len(found) >= limit:
                break
    found.sort(key=lambda r: str(r.get("account_name") or ""))
    return found


def recovery_stats() -> dict[str, Any]:
    """Сколько строк в картотеке и когда её последний раз обновляли."""
    with connect() as conn:
        r = rows(conn.execute(_q(
            "SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM account_recovery")))
    # Ключ НЕ "rows": в ответе API рядом лежит список найденных строк под
    # тем же именем, и счётчик молча затирал бы его.
    return {"total": r[0]["n"] if r else 0,
            "updated_at": r[0]["last"] if r else ""}


def get_recovery(session_name: str) -> dict[str, Any] | None:
    """Данные восстановления с расшифровкой секретов — для показа владельцу."""
    import json as _json
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT * FROM account_recovery WHERE session_name = ?"),
            (session_name,)))
    if not found:
        return None
    r = found[0]
    try:
        secret = _json.loads(decrypt(r.pop("secret_encrypted")) or "{}")
    except Exception:  # noqa: BLE001
        secret = {}
    r["secret"] = secret
    return r


# --------------------------------------------------------------------------- #
# Keitaro
# --------------------------------------------------------------------------- #
def replace_keitaro_campaigns(rows_in: list[dict[str, Any]]) -> int:
    """Перезалить справочник кампаний трекера.

    Именно перезалить, а не до-обновить: кампании в Keitaro удаляют и
    переименовывают, и оставлять у себя исчезнувшую значит потом сопоставить
    объявление с тем, чего уже нет.
    """
    stamp = now()
    with connect() as conn:
        conn.execute(_q("DELETE FROM keitaro_campaigns"))
        for r in rows_in:
            if not r.get("kt_id"):
                continue
            conn.execute(_q("""
                INSERT INTO keitaro_campaigns
                    (kt_id, alias, name, grp, domain, host, state, token,
                     traffic_source_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """), (r["kt_id"], r.get("alias", ""), r.get("name", ""),
                   r.get("grp", ""), r.get("domain", ""), r.get("host", ""),
                   r.get("state", ""), r.get("token", ""),
                   int(r.get("traffic_source_id") or 0), stamp))
    return len(rows_in)


def keitaro_campaigns() -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q("SELECT * FROM keitaro_campaigns")))


def set_ad_keitaro(ad_id: str, act_id: str, campaign_id: str, link: str,
                   kt_id: int, matched_by: str) -> None:
    """Запомнить связь объявления с кампанией трекера.

    Пишем и ненайденные (`kt_id = 0`) тоже: «не сопоставилось и вот почему»
    — такой же результат, как и найденное, и без него нельзя ответить,
    насколько полно покрытие.
    """
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO ad_keitaro
                (ad_id, act_id, campaign_id, link, kt_id, matched_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ad_id) DO UPDATE SET
                act_id = ?, campaign_id = ?, link = ?, kt_id = ?,
                matched_by = ?, updated_at = ?
        """), (ad_id, act_id, campaign_id, link, int(kt_id), matched_by, now(),
               act_id, campaign_id, link, int(kt_id), matched_by, now()))


def ads_with_links() -> list[dict[str, Any]]:
    """Объявления, у которых есть ссылка — кандидаты на сопоставление."""
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT a.ad_id, a.act_id, a.campaign_id, a.effective_status,
                   m.link_url AS link
              FROM ads a
              JOIN ads_meta m ON m.ad_id = a.ad_id
             WHERE COALESCE(m.link_url, '') != ''
        """)))


def keitaro_for_campaign(campaign_id: str) -> list[dict[str, Any]]:
    """Кампании трекера, на которые льёт эта кампания Facebook.

    Обычно одна, но бывает и несколько: в кампании живут разные объявления,
    и никто не мешает завести им разные ссылки.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT k.kt_id, k.name, k.alias, k.domain, k.state, k.grp,
                   COUNT(*) AS ads, MIN(ak.matched_by) AS matched_by
              FROM ad_keitaro ak
              JOIN keitaro_campaigns k ON k.kt_id = ak.kt_id
             WHERE ak.campaign_id = ? AND ak.kt_id != 0
             GROUP BY k.kt_id, k.name, k.alias, k.domain, k.state, k.grp
             ORDER BY ads DESC
        """), (campaign_id,)))


def keitaro_gaps(campaign_id: str) -> list[dict[str, Any]]:
    """Объявления кампании БЕЗ кампании трекера — по доменам.

    Отдаём отдельно от найденного, потому что это не «нет данных», а
    рабочая проблема: живое объявление на домене, которого нет в трекере,
    льёт в пустоту. `live` считаем по статусам, при которых показ идёт.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT ak.link, COUNT(*) AS ads,
                   SUM(CASE WHEN UPPER(COALESCE(a.effective_status, '')) IN
                        ('ACTIVE', 'WITH_ISSUES') THEN 1 ELSE 0 END) AS live,
                   MIN(ak.matched_by) AS matched_by
              FROM ad_keitaro ak
              LEFT JOIN ads a ON a.ad_id = ak.ad_id
             WHERE ak.campaign_id = ? AND ak.kt_id = 0
               AND ak.matched_by NOT LIKE '%соцсет%'
             GROUP BY ak.link
             ORDER BY live DESC, ads DESC
        """), (campaign_id,)))


def keitaro_map(act_id: str) -> dict[str, dict[str, Any]]:
    """Сводка по кампаниям кабинета: с чем связана и где связи нет.

    Нужна списку кампаний: пробегать по каждой отдельным запросом ради
    значка — это десятки запросов на один экран.
    """
    with connect() as conn:
        found = rows(conn.execute(_q("""
            SELECT ak.campaign_id, ak.kt_id, ak.matched_by,
                   COUNT(*) AS ads,
                   SUM(CASE WHEN UPPER(COALESCE(a.effective_status, '')) IN
                        ('ACTIVE', 'WITH_ISSUES') THEN 1 ELSE 0 END) AS live
              FROM ad_keitaro ak
              LEFT JOIN ads a ON a.ad_id = ak.ad_id
             WHERE ak.act_id = ?
             GROUP BY ak.campaign_id, ak.kt_id, ak.matched_by
        """), (act_id,)))
    # Сами ссылки — отдельным запросом, сгруппированные по адресу: на уровне
    # кампании человек хочет видеть, КУДА она льёт, не разворачивая дерево
    # до объявлений. Одна кампания обычно льёт на один адрес, и тогда это
    # одна строка; если адресов несколько — это само по себе повод
    # присмотреться.
    with connect() as conn:
        linkrows = rows(conn.execute(_q("""
            SELECT campaign_id, link, kt_id, COUNT(*) AS ads
              FROM ad_keitaro
             WHERE act_id = ? AND COALESCE(link, '') != ''
             GROUP BY campaign_id, link, kt_id
             ORDER BY ads DESC
        """), (act_id,)))
    names = {k["kt_id"]: k for k in keitaro_campaigns()}
    out: dict[str, dict[str, Any]] = {}
    for row in found:
        cid = row["campaign_id"]
        slot = out.setdefault(cid, {"kt": [], "gap_ads": 0, "gap_live": 0,
                                    "social": 0})
        if row["kt_id"]:
            kt = names.get(row["kt_id"]) or {}
            slot["kt"].append({"kt_id": row["kt_id"],
                               "name": kt.get("name", ""),
                               "ads": row["ads"]})
        elif "соцсет" in str(row.get("matched_by") or ""):
            # Объявление ведёт в профиль или пост: кампании в трекере у него
            # нет по определению, и подсвечивать это как пробел — врать.
            slot["social"] += row["ads"]
        else:
            slot["gap_ads"] += row["ads"]
            slot["gap_live"] += row["live"] or 0
    for row in linkrows:
        slot = out.setdefault(row["campaign_id"],
                              {"kt": [], "gap_ads": 0, "gap_live": 0,
                               "social": 0})
        slot.setdefault("links", []).append(
            {"link": row["link"], "ads": row["ads"], "kt_id": row["kt_id"]})
    return out


def _clean_link(url: str) -> str:
    """Ссылка без параметров: только схема, хост и путь.

    Параметры выбрасываем целиком по двум причинам. Первая: там одни макросы
    вида `{{ad.id}}`, которые до клика ничего не значат, а в выгрузке только
    мешают. Вторая важнее — среди них ездит `fbtoken`, рабочий токен
    Facebook, и таскать его в отчёты, файлы и переписку незачем.
    """
    text = str(url or "").strip()
    for sep in ("?", "#"):
        if sep in text:
            text = text.split(sep, 1)[0]
    return text


def keitaro_live_links(since: str = "", until: str = "",
                       marketer_id: str = "",
                       act_ids: list[str] | None = None,
                       limit: int = 500, offset: int = 0
                       ) -> list[dict[str, Any]]:
    """Три колонки: id кампании Keitaro, ссылка, расход из Facebook.

    Больше ничего намеренно. Это выгрузка для сведения с выручкой трекера, и
    лишние поля в ней только мешают: принимающая сторона ищет ключ и сумму.

    Без дат считается ВЕСЬ открут, что у нас есть. Даты необязательны:
    кампания трекера живёт дольше отчётного окна, и «сколько на неё ушло
    всего» — более частый вопрос, чем «сколько за неделю».

    Берём только живые объявления (`ACTIVE`, `WITH_ISSUES`) и только те, у
    которых связь с трекером найдена. Остановленное объявление выручки уже
    не принесёт, несопоставленное принесёт её мимо нас — в обоих случаях
    строка испортила бы расчёт молча.
    """
    where = ["ak.kt_id != 0",
             "UPPER(COALESCE(a.effective_status, '')) IN ('ACTIVE', 'WITH_ISSUES')"]
    args: list[Any] = []
    # Фильтр по датам живёт в ON у ad_daily, а не в WHERE: иначе объявление
    # без открута за период выпало бы вместе со своей связкой, хотя связка
    # от этого никуда не делась.
    on_dates = ""
    if since and until:
        on_dates = " AND d.date >= ? AND d.date <= ?"
        args.extend([since, until])
    if act_ids:
        where.append("ak.act_id IN (" + ",".join("?" * len(act_ids)) + ")")
        args.extend(act_ids)
    if marketer_id:
        # EXISTS, а не JOIN: у кабинета бывает несколько записей о владении
        # (смена владельца — это новая строка), и джойн размножил бы каждый
        # день открута по их числу. На живых данных расход ровно удваивался.
        where.append("EXISTS (SELECT 1 FROM ad_account_owner o"
                     " WHERE o.act_id = ak.act_id AND o.marketer_id = ?)")
        args.append(marketer_id)
    with connect() as conn:
        raw = rows(conn.execute(_q(f"""
            SELECT ak.kt_id AS keitaro_id,
                   ak.link  AS link,
                   COALESCE(SUM(d.spend), 0) AS spend
              FROM ad_keitaro ak
              JOIN ads a ON a.ad_id = ak.ad_id
              LEFT JOIN ad_daily d ON d.ad_id = ak.ad_id{on_dates}
             WHERE {' AND '.join(where)}
             GROUP BY ak.kt_id, ak.link
        """), tuple(args)))

    # Схлопываем уже по ОЧИЩЕННОЙ ссылке: один и тот же адрес с разным
    # набором макросов — это одна строка, а не три. Делаем это в Python,
    # потому что обрезка до знака вопроса в SQLite и Postgres пишется
    # по-разному, а расходиться в поведении между ними нельзя.
    merged: dict[tuple[int, str], dict[str, Any]] = {}
    for r in raw:
        key = (int(r["keitaro_id"] or 0), _clean_link(r["link"]))
        slot = merged.setdefault(key, {"keitaro_id": key[0], "link": key[1],
                                       "spend": 0.0})
        slot["spend"] += float(r["spend"] or 0)
    out = sorted(merged.values(), key=lambda r: -r["spend"])
    for r in out:
        r["spend"] = round(r["spend"], 2)
    return out[offset:offset + limit] if limit else out[offset:]


def creative_rows(since: str = "", until: str = "",
                  marketer_id: str = "", act_ids: list[str] | None = None,
                  only_active: bool = False,
                  limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
    """Открут в разрезе КРЕАТИВА, а не объявления.

    Один креатив живёт в нескольких объявлениях: маркетолог дублирует
    объявление на другой адсет, и расход по одному и тому же ролику
    расползается по строкам. Здесь он собран обратно.

    Сумма по всем креативам меньше общего открута кабинета: у объявлений,
    поднятых из существующего поста, `creative.id` бывает пуст. Такие строки
    честнее показать отдельно, чем растворить в «прочем» — они попадают в
    выдачу с `creative_id = ""` и своим именем объявления.
    """
    where = ["1=1"]
    args: list[Any] = []
    # Даты — в ON, не в WHERE: креатив без открута за период должен остаться
    # в списке с нулём, а не исчезнуть вместе со своей карточкой.
    on_dates = ""
    if since and until:
        on_dates = " AND d.date >= ? AND d.date <= ?"
        args.extend([since, until])
    if only_active:
        where.append("UPPER(COALESCE(a.effective_status, ''))"
                     " IN ('ACTIVE', 'WITH_ISSUES')")
    if act_ids:
        where.append("a.act_id IN (" + ",".join("?" * len(act_ids)) + ")")
        args.extend(act_ids)
    if marketer_id:
        # EXISTS, а не JOIN: у кабинета бывает несколько записей о владении,
        # и джойн размножил бы каждый день открута по их числу.
        where.append("EXISTS (SELECT 1 FROM ad_account_owner o"
                     " WHERE o.act_id = a.act_id AND o.marketer_id = ?)")
        args.append(marketer_id)
    with connect() as conn:
        raw = rows(conn.execute(_q(f"""
            SELECT COALESCE(m.creative_id, '')    AS creative_id,
                   a.ad_id                        AS ad_id,
                   COALESCE(a.name, '')           AS ad_name,
                   COALESCE(m.title, '')          AS title,
                   COALESCE(m.thumb_url, '')      AS thumb_url,
                   COALESCE(m.preview_url, '')    AS preview_url,
                   COALESCE(m.object_type, '')    AS object_type,
                   COALESCE(a.effective_status,'') AS status,
                   a.act_id                       AS act_id,
                   COALESCE(SUM(d.spend), 0)       AS spend,
                   COALESCE(SUM(d.impressions), 0) AS impressions,
                   COALESCE(SUM(d.clicks), 0)      AS clicks,
                   MIN(d.date)                    AS first_day,
                   MAX(d.date)                    AS last_day
              FROM ads a
              LEFT JOIN ads_meta m ON m.ad_id = a.ad_id
              LEFT JOIN ad_daily d ON d.ad_id = a.ad_id{on_dates}
             WHERE {' AND '.join(where)}
             GROUP BY a.ad_id
        """), tuple(args)))

    # Схлопывание по креативу делаем в Python: у объявлений без creative_id
    # ключом становится сам ad_id, и выразить это одним GROUP BY, одинаково
    # работающим в SQLite и Postgres, заметно менее читаемо.
    merged: dict[str, dict[str, Any]] = {}
    for r in raw:
        cid = (r["creative_id"] or "").strip()
        key = cid or f"ad:{r['ad_id']}"
        slot = merged.get(key)
        if slot is None:
            slot = merged[key] = {
                "creative_id": cid,
                "name": r["ad_name"] or r["title"] or "",
                "title": r["title"] or "",
                "object_type": r["object_type"] or "",
                "thumb_url": r["thumb_url"] or "",
                "preview_url": r["preview_url"] or "",
                "act_ids": set(), "ads": 0, "active": 0,
                "spend": 0.0, "impressions": 0, "clicks": 0,
                "first_day": "", "last_day": "",
            }
        slot["ads"] += 1
        slot["act_ids"].add(r["act_id"])
        if (r["status"] or "").upper() in ("ACTIVE", "WITH_ISSUES"):
            slot["active"] += 1
        slot["spend"] += float(r["spend"] or 0)
        slot["impressions"] += int(r["impressions"] or 0)
        slot["clicks"] += int(r["clicks"] or 0)
        for fld, pick in (("first_day", min), ("last_day", max)):
            day = r[fld] or ""
            if day:
                slot[fld] = pick(slot[fld], day) if slot[fld] else day

    out = sorted(merged.values(), key=lambda r: -r["spend"])
    for r in out:
        r["spend"] = round(r["spend"], 2)
        r["accounts"] = len(r.pop("act_ids"))
        imp, clk = r["impressions"], r["clicks"]
        # CTR и CPM считаем здесь, а не на фронте: одна формула на все
        # места вывода — таблицу, выгрузку и API.
        r["ctr"] = round(clk * 100.0 / imp, 2) if imp else 0.0
        r["cpm"] = round(r["spend"] * 1000.0 / imp, 2) if imp else 0.0
    return out[offset:offset + limit] if limit else out[offset:]


def keitaro_coverage() -> dict[str, Any]:
    """Насколько полно объявления сопоставлены с трекером."""
    with connect() as conn:
        total = rows(conn.execute(_q("SELECT COUNT(*) AS n FROM ad_keitaro")))
        ok = rows(conn.execute(_q(
            "SELECT COUNT(*) AS n FROM ad_keitaro WHERE kt_id != 0")))
        why = rows(conn.execute(_q("""
            SELECT matched_by, COUNT(*) AS n FROM ad_keitaro
             GROUP BY matched_by ORDER BY n DESC""")))
    return {"ads": total[0]["n"] if total else 0,
            "matched": ok[0]["n"] if ok else 0,
            "by_reason": why}


def total_spend_raw(since: str, until: str, act_ids: list[str] | None = None
                    ) -> Decimal:
    """Суммарный FB-спенд в исходной валюте (не в базовой).

    Нужен там, где сравнение идёт с суммами в долларах — например со
    списаниями по картам, — а не с пересчётом в рубли.
    """
    with connect() as conn:
        if act_ids:
            marks = ",".join("?" * len(act_ids))
            r = rows(conn.execute(_q(f"""
                SELECT COALESCE(SUM(spend), 0) AS s FROM spend_daily
                 WHERE date >= ? AND date <= ? AND act_id IN ({marks})
            """), (since, until, *act_ids)))
        else:
            r = rows(conn.execute(_q("""
                SELECT COALESCE(SUM(spend), 0) AS s FROM spend_daily
                 WHERE date >= ? AND date <= ?
            """), (since, until)))
    return dec(r[0]["s"]) if r else ZERO


# --------------------------------------------------------------------------- #
# Пул прокси
# --------------------------------------------------------------------------- #
def _proxy_hint(url: str) -> str:
    """host:port со скрытым логином-паролем — чтобы узнать прокси в списке,
    не показывая учётку."""
    import re as _re
    m = _re.match(r"(\w+)://(?:[^@/]+@)?([^/:]+)(?::(\d+))?", url.strip())
    if not m:
        return url[:24]
    scheme, host, port = m.group(1), m.group(2), m.group(3) or ""
    return f"{scheme}://…@{host}" + (f":{port}" if port else "")


def _proxy_fingerprint(url: str) -> str:
    import hashlib
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]


def add_proxy(url: str, label: str = "", marketer_id: str = "") -> str:
    """Завести прокси в пул. Дубль (тот же URL) не создаётся — вернётся
    существующий id, чтобы список не засорялся копиями.

    Бесхозный дубль достаётся тому, кто первым его назвал: прокси, заведённые
    до появления владельцев, иначе остались бы не видны никому, кроме админа.
    Чужой не переприсваивается никогда.
    """
    url = (url or "").strip()
    if not url:
        return ""
    fp = _proxy_fingerprint(url)
    with connect() as conn:
        found = rows(conn.execute(
            _q("SELECT id, marketer_id FROM proxies WHERE fingerprint = ?"), (fp,)))
        if found:
            if marketer_id and not (found[0]["marketer_id"] or ""):
                conn.execute(_q("UPDATE proxies SET marketer_id = ? WHERE id = ?"),
                             (marketer_id, found[0]["id"]))
            return found[0]["id"]
        pid = new_id()
        conn.execute(_q("""
            INSERT INTO proxies (id, marketer_id, label, hint, fingerprint,
                                 url_encrypted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """), (pid, marketer_id or "", label or _proxy_hint(url), _proxy_hint(url),
               fp, encrypt(url), now()))
    return pid


def list_proxies(marketer_id: str | None = None) -> list[dict[str, Any]]:
    """Пул для выпадающего списка — БЕЗ расшифровки (только подсказка).

    `None` — весь пул, так смотрит админ. Со значением возвращаются только
    свои: чужие адреса в списке — это чужие учётки, пусть и за звёздочками.
    """
    sql = "SELECT id, label, hint, created_at FROM proxies"
    params: tuple[Any, ...] = ()
    if marketer_id is not None:
        sql += " WHERE marketer_id = ?"
        params = (marketer_id,)
    sql += " ORDER BY label"
    with connect() as conn:
        return rows(conn.execute(_q(sql), params))


def get_proxy_url(proxy_id: str, marketer_id: str | None = None) -> str:
    """URL прокси по id. С `marketer_id` — только если прокси его.

    Проверка здесь, а не в интерфейсе: id приходит от браузера, и «в списке
    этого не было» ничего не закрывает.
    """
    if not proxy_id:
        return ""
    sql = "SELECT url_encrypted FROM proxies WHERE id = ?"
    params: tuple[Any, ...] = (proxy_id,)
    if marketer_id is not None:
        sql += " AND marketer_id = ?"
        params = (proxy_id, marketer_id)
    with connect() as conn:
        found = rows(conn.execute(_q(sql), params))
    return decrypt(found[0]["url_encrypted"]) if found else ""


def resolve_proxy(proxy_id: str = "", proxy_url: str = "",
                  marketer_id: str | None = None) -> str:
    """Вернуть URL прокси: из пула по id либо новый (заодно сохранив в пул).

    Новый введённый прокси всегда попадает в пул — чтобы в следующий раз
    его можно было выбрать из списка, а не вводить заново.
    """
    proxy_url = (proxy_url or "").strip()
    if proxy_url:
        add_proxy(proxy_url, marketer_id=marketer_id or "")
        return proxy_url
    return get_proxy_url(proxy_id, marketer_id)


def _proxy_readable(url: str) -> bool:
    """Сможет ли requests прочитать этот адрес.

    Проверяем именно так, а не «похоже ли на URL»: ошибка «Failed to parse»
    приходит из той же самой функции разбора, и повторять её логику словами
    значит рано или поздно с ней разойтись.
    """
    import urllib.parse as _up
    try:
        parsed = _up.urlparse(url)
        return bool(parsed.hostname) and parsed.port is not None
    except ValueError:
        return False


def repair_proxy_formats() -> int:
    """Разовый ремонт адресов, сохранённых до того, как мы научились читать
    формат поставщиков (`socks5://хост:порт:логин:пароль`).

    Сам прокси не меняется: те же схема, хост, порт, логин и пароль — только
    переставлены в синтаксис, который умеет разобрать requests. Трогаем
    ИСКЛЮЧИТЕЛЬНО те записи, которые сейчас не читаются вовсе: рабочий адрес
    переписывать не за чем и незачем.
    """
    from core.graph import parse_proxy
    fixed = 0
    with connect() as conn:
        idents = rows(conn.execute(_q(
            "SELECT id, proxy_encrypted FROM identities"
            " WHERE proxy_encrypted IS NOT NULL")))
        for row in idents:
            try:
                url = decrypt(row["proxy_encrypted"])
            except Exception:  # noqa: BLE001 — чужой ключ: не наше дело
                continue
            if not url or _proxy_readable(url):
                continue
            good, err = parse_proxy(url)
            if err or not good or good == url:
                continue
            conn.execute(_q("UPDATE identities SET proxy_encrypted = ? WHERE id = ?"),
                         (encrypt(good), row["id"]))
            fixed += 1

        pool = rows(conn.execute(_q(
            "SELECT id, url_encrypted, label, hint, fingerprint FROM proxies")))
        seen = {r["fingerprint"]: r["id"] for r in pool}
        for row in pool:
            try:
                url = decrypt(row["url_encrypted"])
            except Exception:  # noqa: BLE001
                continue
            if not url or _proxy_readable(url):
                continue
            good, err = parse_proxy(url)
            if err or not good or good == url:
                continue
            fp = _proxy_fingerprint(good)
            if fp in seen and seen[fp] != row["id"]:
                # Тот же адрес уже лежит в пуле в правильном написании —
                # чинить нечего, надо убрать копию, иначе в выпадающем
                # списке будут два одинаковых пункта.
                conn.execute(_q("DELETE FROM proxies WHERE id = ?"), (row["id"],))
                fixed += 1
                continue
            hint = _proxy_hint(good)
            label = row["label"] if row["label"] != row["hint"] else hint
            conn.execute(_q("""
                UPDATE proxies SET url_encrypted = ?, hint = ?, label = ?,
                                   fingerprint = ? WHERE id = ?
            """), (encrypt(good), hint, label, fp, row["id"]))
            seen[fp] = row["id"]
            fixed += 1
    return fixed


def claim_orphan_proxies() -> int:
    """Раздать владельцев прокси, заведённым до того, как владельцы появились.

    Ищем по личикам: если прокси личики совпадает с прокси из пула и пул им
    ещё не владеет — хозяин личики и есть хозяин прокси. Спорные (один адрес
    у разных людей) не трогаем: там правильного ответа нет, и пусть остаются
    у админа.
    """
    with connect() as conn:
        pool = rows(conn.execute(_q(
            "SELECT id, fingerprint FROM proxies"
            " WHERE marketer_id IS NULL OR marketer_id = ''")))
        if not pool:
            return 0
        idents = rows(conn.execute(_q(
            "SELECT marketer_id, proxy_encrypted FROM identities"
            " WHERE proxy_encrypted IS NOT NULL")))
    owners: dict[str, set[str]] = {}
    for row in idents:
        try:
            url = decrypt(row["proxy_encrypted"])
        except Exception:  # noqa: BLE001 — нет ключа: молча пропускаем
            continue
        if not url:
            continue
        owners.setdefault(_proxy_fingerprint(url), set()).add(row["marketer_id"])
    taken = 0
    with connect() as conn:
        for p in pool:
            who = owners.get(p["fingerprint"]) or set()
            if len(who) == 1:
                conn.execute(_q("UPDATE proxies SET marketer_id = ? WHERE id = ?"),
                             (next(iter(who)), p["id"]))
                taken += 1
    return taken


def set_all_pages_autoclean(marketer_id: str | None, on: bool) -> int:
    """Массово включить/выключить автоочистку комментов на всех страницах.

    Ровно то массовое действие, ради которого не нужно щёлкать каждую
    страницу: у маркетолога ситуация на всех кабинетах обычно одна.
    """
    with connect() as conn:
        if marketer_id:
            cur = conn.execute(_q(
                "UPDATE page_tokens SET auto_clean = ? WHERE marketer_id = ?"),
                (1 if on else 0, marketer_id))
        else:
            cur = conn.execute(_q("UPDATE page_tokens SET auto_clean = ?"),
                               (1 if on else 0,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


# --------------------------------------------------------------------------- #
# Page-токены и модерация комментариев
# --------------------------------------------------------------------------- #
def save_page_token(page_id: str, name: str, token: str, identity_id: str,
                    marketer_id: str | None, can_moderate: bool) -> None:
    """Сохранить page-токен (шифрованным), не трогая настройку auto_clean.

    auto_clean — согласие владельца, его выставляет человек, а не сбор.
    Поэтому при обновлении токена флаг сохраняется, а не сбрасывается.
    """
    stamp = now()
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO page_tokens (page_id, name, token_encrypted, identity_id,
                                     marketer_id, can_moderate, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (page_id) DO UPDATE SET
                name = ?, token_encrypted = ?, identity_id = ?,
                marketer_id = ?, can_moderate = ?, updated_at = ?
        """), (page_id, name, encrypt(token), identity_id, marketer_id,
               1 if can_moderate else 0, stamp,
               name, encrypt(token), identity_id, marketer_id,
               1 if can_moderate else 0, stamp))


def list_pages(marketer_id: str | None = None) -> list[dict[str, Any]]:
    """Страницы БЕЗ расшифровки токенов — для экрана."""
    sql = """SELECT pt.page_id, pt.name, pt.identity_id, pt.marketer_id,
                    pt.can_moderate, pt.auto_clean, pt.last_cleaned_at,
                    pt.last_deleted, m.name AS owner_name
               FROM page_tokens pt
               LEFT JOIN marketers m ON m.id = pt.marketer_id"""
    params: tuple = ()
    if marketer_id:
        sql += " WHERE pt.marketer_id = ?"
        params = (marketer_id,)
    sql += " ORDER BY pt.name"
    with connect() as conn:
        return rows(conn.execute(_q(sql), params))


def get_page(page_id: str) -> dict[str, Any] | None:
    """Страница С расшифрованным токеном — для модерации."""
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT * FROM page_tokens WHERE page_id = ?"), (page_id,)))
    if not found:
        return None
    row = found[0]
    row["token"] = decrypt(row.pop("token_encrypted"))
    return row


def set_page_autoclean(page_id: str, on: bool) -> None:
    with connect() as conn:
        conn.execute(_q("UPDATE page_tokens SET auto_clean = ? WHERE page_id = ?"),
                     (1 if on else 0, page_id))


def mark_page_cleaned(page_id: str, deleted: int) -> None:
    with connect() as conn:
        conn.execute(_q("""UPDATE page_tokens
                              SET last_cleaned_at = ?, last_deleted = ?
                            WHERE page_id = ?"""), (now(), deleted, page_id))


def autoclean_pages() -> list[dict[str, Any]]:
    """Страницы, где владелец включил автоочистку — для фонового цикла."""
    with connect() as conn:
        found = rows(conn.execute(
            "SELECT page_id FROM page_tokens WHERE auto_clean = 1"))
    return [get_page(r["page_id"]) for r in found]


def billing_forecast(act_id: str) -> dict[str, Any]:
    """Когда FB спишет с кабинета — оценка по машинным фактам.

    FB списывает по двум правилам, что раньше: либо несписанный долг
    достигает биллингового порога, либо наступает плановая дата счёта
    (`next_bill_date`). Всё нужное отдаёт сам FB, маркетолог на это не влияет:

      • `balance` — уже накопленный несписанный долг;
      • `next_bill_date` — плановая дата;
      • порог — из истории: FB списывал ровно на нём, поэтому берём
        максимум недавних списаний как оценку текущего порога;
      • дневной темп — средний открут за последнюю неделю.

    Возвращает ближайшую из двух дат, ожидаемую сумму и по какому правилу.
    Это оценка: порог FB поднимает со временем, а точную его величину API
    не отдаёт — поэтому честно помечаем как приблизительную.
    """
    acc = get_ad_account(act_id)
    if not acc:
        return {"known": False}
    balance = dec(acc.get("balance"))
    today = date.today()

    # Порог — максимум недавних списаний. Если истории нет, берём
    # консервативные $25 (типичный стартовый порог нового кабинета).
    with connect() as conn:
        charges = rows(conn.execute(_q("""
            SELECT amount FROM fb_transactions
             WHERE act_id = ? AND amount > 0
             ORDER BY time DESC LIMIT 20
        """), (act_id,)))
        recent = rows(conn.execute(_q("""
            SELECT COALESCE(SUM(spend), 0) AS s, COUNT(DISTINCT date) AS d
              FROM spend_daily
             WHERE act_id = ? AND date >= ?
        """), (act_id, (today - timedelta(days=7)).isoformat())))
    threshold = max((dec(c["amount"]) for c in charges), default=ZERO)
    if threshold <= ZERO:
        threshold = Decimal("25")
    days_active = int(recent[0]["d"] or 0)
    daily = (dec(recent[0]["s"]) / days_active) if days_active else ZERO

    # По порогу: сколько ещё открутить до срабатывания, делённое на темп.
    by_threshold = None
    if daily > ZERO:
        remain = threshold - balance
        days = max(0, float(remain / daily)) if remain > ZERO else 0
        by_threshold = today + timedelta(days=round(days))

    # По плановой дате.
    by_date = None
    raw = str(acc.get("next_bill_date") or "")[:10]
    if raw:
        try:
            by_date = date.fromisoformat(raw)
        except ValueError:
            by_date = None

    # Ближайшее из двух.
    when, rule, amount = None, "", ZERO
    if by_threshold and (not by_date or by_threshold <= by_date):
        when, rule = by_threshold, "порог"
        amount = threshold
    elif by_date:
        when, rule = by_date, "плановый счёт"
        amount = balance + daily * Decimal(max(0, (by_date - today).days))

    return {
        "known": bool(when),
        "when": when.isoformat() if when else "",
        "days_left": (when - today).days if when else None,
        "rule": rule,
        "expected_amount": amount,
        "balance": balance,
        "threshold": threshold,
        "daily": daily,
        "currency": acc.get("currency") or "",
        "next_bill_date": raw,
    }


def match_card_for_account(act_id: str, since: str, until: str
                           ) -> dict[str, Any]:
    """Какая карта фандит кабинет — по машинным фактам, без подписей.

    Маркетолог может назвать карту как угодно и никого не предупредить,
    поэтому title карты, заметки Octo и названия сессий в расчёт не берутся.
    Опора только на то, что генерят сами системы:

      • FB сам пишет last4 фандинга кабинета (`funding_last4`);
      • MyBroCard сам пишет реальные списания: суммы, даты, владельца карты.

    Когда last4 указывает на одну карту — ответ однозначен. Когда на
    несколько (последних цифр всего 10000 на тысячи карт), кандидаты
    ранжируются по СОВПАДЕНИЮ ПОВЕДЕНИЯ: у настоящей карты списания
    ложатся на дни открута кабинета и близки по сумме. Это тоже машинные
    данные с обеих сторон, подделать их маркетолог не может.

    Возвращает лучшую карту, список кандидатов и уровень уверенности:
      exact  — одна карта на этот last4;
      likely — несколько, но одна явно ближе по поведению;
      weak   — несколько, поведение не разделяет (решать человеку).
    """
    account = get_ad_account(act_id)
    if not account:
        return {"confidence": "none", "card": None, "candidates": []}
    last4 = (account.get("funding_last4") or "").strip()
    brand = (account.get("funding_display") or "").upper()
    if not last4:
        return {"confidence": "none", "card": None, "candidates": [],
                "reason": "FB не отдал карту кабинета"}

    with connect() as conn:
        cards = rows(conn.execute(_q("""
            SELECT card_id, card_bin,
                   MAX(user_email) AS user_email, MAX(user_name) AS user_name,
                   MAX(team) AS team,
                   SUM(CASE WHEN state IN ('settled','pending')
                            THEN amount ELSE 0 END) AS charged,
                   COUNT(*) AS payments,
                   MIN(date) AS first_seen, MAX(date) AS last_seen
              FROM card_payments
             WHERE card_last4 = ? AND date >= ? AND date <= ?
             GROUP BY card_id, card_bin
        """), (last4, since, until)))

    spend = dec(account_totals(act_id, since, until)["spend"])
    for c in cards:
        c["charged"] = dec(c["charged"])
        # Бренд из FB (VISA/MC) против BIN карты (4xxxxx=Visa, 5xxxxx=MC) —
        # машинный отсев кандидата другой платёжной системы.
        c["brand_ok"] = _brand_matches(brand, c["card_bin"])
        # Насколько сумма списаний близка к откруту: 1.0 — точно, 0 — далеко.
        c["amount_fit"] = _amount_fit(spend, c["charged"])
        c["score"] = (2 if c["brand_ok"] else 0) + c["amount_fit"]

    if not cards:
        return {"confidence": "none", "card": None, "candidates": [],
                "last4": last4, "reason": "по last4 карт не найдено"}
    cards.sort(key=lambda c: (-c["score"], -float(c["charged"])))

    # Пар-в-пару перекрывает всё: если конкретные списания FB нашли себе
    # пару по сумме и времени на одной карте, это она — независимо от
    # того, сколько карт делят last4.
    pairing = pair_transactions(act_id, since, until)
    if pairing["best"] and pairing["best"]["matches"] >= 2:
        pid = pairing["best"]["card_id"]
        chosen = next((c for c in cards if c["card_id"] == pid), None)
        if chosen:
            chosen["paired"] = pairing["best"]["matches"]
            return {"confidence": "exact", "card": chosen, "candidates": cards,
                    "last4": last4, "account_spend": spend,
                    "method": "transactions", "paired": pairing["best"]["matches"]}

    best = cards[0]
    if len(cards) == 1:
        conf = "exact"
    elif best["score"] - cards[1]["score"] >= 1.0:
        conf = "likely"
    else:
        conf = "weak"
    return {"confidence": conf, "card": best, "candidates": cards,
            "last4": last4, "account_spend": spend, "method": "behavior"}


def _brand_matches(fb_display: str, card_bin: str) -> bool:
    """VISA/MASTERCARD из FB против первой цифры BIN карты."""
    bin1 = (card_bin or "")[:1]
    if "VISA" in fb_display:
        return bin1 == "4"
    if "MASTER" in fb_display or "MC" in fb_display:
        return bin1 == "5"
    return True  # бренд неизвестен — не отсеиваем


def _amount_fit(spend: Decimal, charged: Decimal) -> float:
    """Близость суммы списаний к откруту кабинета, 0..1.

    Не требуем точного равенства: комиссии, лаг расчётов и порог списания
    FB дают законное расхождение. Совпадение порядка величины уже сильный
    сигнал, что это та самая карта.
    """
    spend, charged = abs(spend), abs(charged)
    if spend == 0 and charged == 0:
        return 0.5
    hi = max(spend, charged)
    if hi == 0:
        return 0.0
    return float(1 - abs(spend - charged) / hi)


def upsert_card_payment(r: dict[str, Any]) -> None:
    stamp = now()
    fields = ("payment_id", "card_id", "card_last4", "card_bin", "card_title",
              "user_email", "user_name", "user_id", "team", "marketer_id", "merchant",
              "descriptor", "state", "decline", "is_micro", "currency",
              "amount", "fee", "date", "date_final")
    values = [r.get(f) for f in fields]
    updates = ", ".join(f"{f} = ?" for f in fields[1:])
    with connect() as conn:
        conn.execute(_q(f"""
            INSERT INTO card_payments ({', '.join(fields)}, updated_at)
            VALUES ({', '.join('?' * len(fields))}, ?)
            ON CONFLICT (payment_id) DO UPDATE SET {updates}, updated_at = ?
        """), (*values, stamp, *values[1:], stamp))


def upsert_fb_transaction(r: dict[str, Any]) -> None:
    stamp = now()
    payload = (r["tx_id"], r["act_id"], r["time"], r.get("charge_type", ""),
               r.get("status", ""), r.get("payment_option", ""),
               r.get("currency", ""), r.get("amount", 0), r.get("tax_amount", 0))
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO fb_transactions
                (tx_id, act_id, time, charge_type, status, payment_option,
                 currency, amount, tax_amount, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tx_id) DO UPDATE SET
                act_id = ?, time = ?, charge_type = ?, status = ?,
                payment_option = ?, currency = ?, amount = ?,
                tax_amount = ?, updated_at = ?
        """), (*payload, stamp, *payload[1:], stamp))


# --------------------------------------------------------------------------- #
# Курсы валют
# --------------------------------------------------------------------------- #
def set_rate(day: str, currency: str, rate_to_base: Any, source: str = "") -> None:
    """Курс: сколько единиц базовой валюты стоит 1 единица `currency`."""
    with connect() as conn:
        conn.execute(_q("""
            INSERT INTO fx_rates (date, currency, rate_to_base, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (date, currency) DO UPDATE SET
                rate_to_base = ?, source = ?, updated_at = ?
        """), (day, currency.upper(), str(rate_to_base), source, now(),
               str(rate_to_base), source, now()))


def fill_rate_gaps(currency: str, since: str, until: str) -> int:
    """Уплотнить курсы: перенести последний известный вперёд на дни без котировки.

    Плотная таблица нужна, чтобы пересчёт в отчёте был обычным JOIN. Иначе
    выходные и праздники дают дырки, а дырка в курсе — это молчаливо потерянный
    спенд за эти дни.
    """
    currency = currency.upper()
    with connect() as conn:
        cur = conn.execute(_q("""
            SELECT date, rate_to_base FROM fx_rates
             WHERE currency = ? AND date <= ? ORDER BY date
        """), (currency, until))
        known = {r["date"]: r["rate_to_base"] for r in rows(cur)}

    day = date.fromisoformat(since)
    end = date.fromisoformat(until)
    last: Any = None
    for iso, value in sorted(known.items()):
        if iso <= since:
            last = value
    filled = 0
    while day <= end:
        iso = day.isoformat()
        if iso in known:
            last = known[iso]
        elif last is not None:
            set_rate(iso, currency, last, source="carry-forward")
            filled += 1
        day += timedelta(days=1)
    return filled


# --------------------------------------------------------------------------- #
# Витрина фаундера: маркетолог → цикл → гео
# --------------------------------------------------------------------------- #
def _attributed_facts(conn: Any, table: str, since: str, until: str,
                      extra_cols: str = "") -> list[dict[str, Any]]:
    """Факты за период с подтянутыми владельцем и курсом.

    Один и тот же JOIN нужен и для итогов, и для гео-детализации, поэтому
    вынесен: разъехавшиеся условия джойна в двух местах = разъехавшиеся цифры.
    """
    # Владелец: сперва формально закреплённый (ad_account_owner) на дату
    # открута, а если его нет — по доступу личики (ad_account_access →
    # identities). Кабинет «лежит у Бориса», даже если владельца ему явно не
    # проставили: открут по нему должен идти Борису, а не в «без владельца».
    cur = conn.execute(_q(f"""
        SELECT s.date, s.act_id, s.currency, s.spend{extra_cols},
               COALESCE(o.marketer_id, acc.marketer_id) AS marketer_id,
               fx.rate_to_base
          FROM {table} s
          LEFT JOIN ad_account_owner o
                 ON o.act_id = s.act_id
                AND s.date >= o.since
                AND (o.until IS NULL OR s.date <= o.until)
          LEFT JOIN (
                 SELECT x.act_id, MIN(i.marketer_id) AS marketer_id
                   FROM ad_account_access x
                   JOIN identities i ON i.id = x.identity_id
                  WHERE i.marketer_id IS NOT NULL AND i.marketer_id <> ''
                  GROUP BY x.act_id
               ) acc ON acc.act_id = s.act_id
          LEFT JOIN fx_rates fx
                 ON fx.date = s.date AND fx.currency = s.currency
         WHERE s.date >= ? AND s.date <= ?
    """), (since, until))
    return rows(cur)


def _to_base(spend: Decimal, currency: str, rate: Any) -> Decimal | None:
    """Пересчёт в базовую валюту. None — курса нет, и это НЕ ноль."""
    if (currency or "").upper() == schema.BASE_CURRENCY:
        return spend
    if rate is None:
        return None
    return spend * dec(rate)


# Порог сверки гео-разбивки с итогом кабинета. Meta округляет разбивку,
# поэтому копеечные расхождения — норма, а не сигнал.
GEO_TOLERANCE = Decimal("0.01")


def founder_tree(cycle_from: int | None = None, cycle_to: int | None = None,
                 *, since: str = "", until: str = "") -> dict[str, Any]:
    """Иерархия фаундера: маркетолог → цикл → гео.

    Субъект отчёта — МАРКЕТОЛОГ. Поэтому итоги по маркетологу и циклу берутся
    из `spend_daily` (открут кабинета за день) — это авторитетный факт. Гео из
    `spend_country_daily` — детализация нижнего уровня, и она не может испортить
    итог: если разбивка по странам не сходится с итогом кабинета, разница
    показывается отдельной строкой «не разнесено», а сумма листьев по-прежнему
    равна итогу родителя.

    Отдельно обрабатываются три случая, каждый из которых в исходных
    заготовках терялся молча:
      • кабинет без владельца     → маркетолог `__unassigned__`;
      • отсутствующий курс        → в `missing_rates`, сумма не занижается нулём;
      • гео-разбивка не сходится  → в `geo_gaps` + строка «не разнесено».
    """
    if since and until:
        # Явные даты: период может быть уже цикла («сегодня», «7 дней»).
        # Группировка по циклам внутри всё равно идёт по дате каждой строки,
        # поэтому неполный цикл просто отдаёт меньше дней, а не расширяется.
        pass
    else:
        cycle_to = cycle_from if cycle_to is None else cycle_to
        since, _ = cycles.cycle_bounds_iso(cycle_from)
        _, until = cycles.cycle_bounds_iso(cycle_to)

    with connect() as conn:
        totals = _attributed_facts(conn, "spend_daily", since, until,
                                   ", s.clicks, s.impressions, s.leads")
        geo_rows = _attributed_facts(conn, "spend_country_daily", since, until,
                                     ", s.country")
        cur = conn.execute(_q("SELECT id, name FROM marketers"))
        names = {r["id"]: r["name"] for r in rows(cur)}

    # --- 1. Сводим гео к ключу (кабинет, дата) --------------------------- #
    geo_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for g in geo_rows:
        geo_by_key.setdefault((g["act_id"], g["date"]), []).append(g)

    # --- 2. Дерево строится ОТ авторитетных итогов ----------------------- #
    tree: dict[str, dict[int, dict[str, dict[str, Any]]]] = {}
    missing_rates: set[tuple[str, str]] = set()
    geo_gaps: list[dict[str, Any]] = []
    orphan_geo: list[tuple[str, str]] = []
    cycle_meta: dict[tuple[str, int], dict[str, Any]] = {}
    total_base = ZERO

    def node_for(marketer: str, cycle: int, country: str) -> dict[str, Any]:
        return (tree.setdefault(marketer, {})
                    .setdefault(cycle, {})
                    .setdefault(country, {"spend_base": ZERO, "unconverted": ZERO,
                                          "clicks": 0, "impressions": 0,
                                          "leads": ZERO, "native": {}}))

    def add(node: dict[str, Any], spend: Decimal, currency: str,
            rate: Any) -> Decimal:
        base = _to_base(spend, currency, rate)
        if base is None:
            node["unconverted"] += spend
            return ZERO
        node["spend_base"] += base
        node["native"][currency] = node["native"].get(currency, ZERO) + spend
        return base

    seen_keys: set[tuple[str, str]] = set()

    for t in totals:
        key = (t["act_id"], t["date"])
        seen_keys.add(key)
        marketer = t["marketer_id"] or UNASSIGNED
        cycle = cycles.cycle_of(date.fromisoformat(t["date"]))
        currency = (t["currency"] or "").upper()
        account_spend = dec(t["spend"])

        if account_spend != ZERO and _to_base(account_spend, currency,
                                              t["rate_to_base"]) is None:
            missing_rates.add((currency or "??", t["date"]))

        # Разносим итог кабинета по странам из детализации.
        details = geo_by_key.get(key, [])
        geo_sum = sum((dec(g["spend"]) for g in details), ZERO)
        residual = account_spend - geo_sum

        # Метрики уровня кабинета — к циклу. Клики и показы по странам берутся
        # из самой разбивки ниже: приписывать их одной стране было бы враньём.
        meta = cycle_meta.setdefault((marketer, cycle),
                                     {"clicks": 0, "impressions": 0, "leads": ZERO})
        meta["clicks"] += int(t.get("clicks") or 0)
        meta["impressions"] += int(t.get("impressions") or 0)
        meta["leads"] += dec(t.get("leads"))

        for g in details:
            node = node_for(marketer, cycle, g["country"] or "??")
            total_base += add(node, dec(g["spend"]), currency, t["rate_to_base"])
            node["clicks"] += int(g.get("clicks") or 0)
            node["impressions"] += int(g.get("impressions") or 0)

        # Остаток (или отсутствие разбивки целиком) — видимой строкой,
        # чтобы сумма листьев равнялась итогу кабинета.
        if residual.copy_abs() > GEO_TOLERANCE:
            node = node_for(marketer, cycle, "—")
            total_base += add(node, residual, currency, t["rate_to_base"])
            if details:
                geo_gaps.append({"act_id": t["act_id"], "date": t["date"],
                                 "account_spend": account_spend,
                                 "geo_sum": geo_sum, "residual": residual,
                                 "currency": currency})

    # --- 3. Гео без итога кабинета — не теряем, но помечаем -------------- #
    for key, details in geo_by_key.items():
        if key in seen_keys:
            continue
        orphan_geo.append(key)
        for g in details:
            marketer = g["marketer_id"] or UNASSIGNED
            cycle = cycles.cycle_of(date.fromisoformat(g["date"]))
            node = node_for(marketer, cycle, g["country"] or "??")
            spend = dec(g["spend"])
            currency = (g["currency"] or "").upper()
            total_base += add(node, spend, currency, g["rate_to_base"])
            if spend != ZERO and _to_base(spend, currency,
                                          g["rate_to_base"]) is None:
                missing_rates.add((currency or "??", g["date"]))

    # --- 4. Сборка ответа ------------------------------------------------ #
    out_marketers = []
    for marketer_id, by_cycle in tree.items():
        out_cycles = []
        for cycle in sorted(by_cycle):
            geos = by_cycle[cycle]
            meta = cycle_meta.get((marketer_id, cycle),
                                  {"clicks": 0, "impressions": 0, "leads": ZERO})
            out_cycles.append({
                "cycle": cycle,
                "label": cycles.label(cycle),
                "spend_base": sum((g["spend_base"] for g in geos.values()), ZERO),
                "unconverted": sum((g["unconverted"] for g in geos.values()), ZERO),
                "clicks": meta["clicks"],
                "impressions": meta["impressions"],
                "leads": meta["leads"],
                "geo": sorted(
                    ({"country": country, **vals} for country, vals in geos.items()),
                    key=lambda g: g["spend_base"], reverse=True),
            })
        out_marketers.append({
            "marketer_id": marketer_id,
            "name": names.get(marketer_id, "Без владельца"
                              if marketer_id == UNASSIGNED else marketer_id),
            "spend_base": sum((c["spend_base"] for c in out_cycles), ZERO),
            "unconverted": sum((c["unconverted"] for c in out_cycles), ZERO),
            "cycles": out_cycles,
        })
    out_marketers.sort(key=lambda m: m["spend_base"], reverse=True)

    return {
        "since": since, "until": until,
        "cycle_from": cycle_from, "cycle_to": cycle_to,
        "base_currency": schema.BASE_CURRENCY,
        "total_base": total_base,
        "missing_rates": sorted(missing_rates),
        "geo_gaps": geo_gaps,
        "geo_without_totals": sorted(orphan_geo),
        "marketers": out_marketers,
    }


def marketer_totals(cycle_from: int, cycle_to: int | None = None
                    ) -> list[dict[str, Any]]:
    """Плоские итоги по маркетологам без гео — для сводки в боте и верхнего
    экрана. Считается из того же авторитетного источника, что и дерево,
    поэтому цифры совпадают по определению."""
    tree = founder_tree(cycle_from, cycle_to)
    return [{"marketer_id": m["marketer_id"], "name": m["name"],
             "spend_base": m["spend_base"], "unconverted": m["unconverted"]}
            for m in tree["marketers"]]


def unassigned_spend(since: str, until: str) -> list[dict[str, Any]]:
    """Кабинеты, у которых есть спенд, но нет владельца на эту дату.

    Считается из `spend_daily` — того же авторитетного источника, что и итоги
    в витрине, иначе список сирот и цифры в отчёте разъедутся.

    Отдельный запрос, потому что это операционная задача («назначь владельца»),
    а не строка в отчёте.
    """
    with connect() as conn:
        cur = conn.execute(_q("""
            SELECT s.act_id, a.name, s.currency,
                   SUM(s.spend) AS spend, MIN(s.date) AS since, MAX(s.date) AS until
              FROM spend_daily s
              LEFT JOIN ad_accounts a ON a.act_id = s.act_id
              LEFT JOIN ad_account_owner o
                     ON o.act_id = s.act_id
                    AND s.date >= o.since
                    AND (o.until IS NULL OR s.date <= o.until)
             WHERE s.date >= ? AND s.date <= ? AND o.marketer_id IS NULL
             GROUP BY s.act_id, a.name, s.currency
             ORDER BY SUM(s.spend) DESC
        """), (since, until))
        return rows(cur)


def link_card_payments(email: str, marketer_id: str) -> int:
    """Привязать платежи с этой почтой к маркетологу. Возвращает число строк.

    Связка «карта → маркетолог» в MyBroCards держится на почте создателя
    карты — это машинный факт, надёжнее сопоставления по last4, где карты
    с одинаковыми последними цифрами существуют. Раньше `marketer_id`
    в платежах оставался пустым у всех 100% строк: интерфейс складывал
    в `marketers.login` имя, а не почту, и совпасть было нечему.
    """
    email = (email or "").strip().lower()
    if not email or not marketer_id:
        return 0
    with connect() as conn:
        cur = conn.execute(_q("""
            UPDATE card_payments SET marketer_id = ?
             WHERE LOWER(user_email) = ? AND (marketer_id IS NULL OR marketer_id = '')
        """), (marketer_id, email))
        return cur.rowcount or 0


def relink_all_card_payments() -> dict[str, int]:
    """Пройти по всем маркетологам и подтянуть их платежи.

    Нужна после заведения людей и как разовая починка исторических данных:
    платежи приходят раньше, чем в системе появляется человек.
    """
    out: dict[str, int] = {}
    for m in list_marketers():
        login = str(m.get("login") or "").strip().lower()
        if "@" in login:
            n = link_card_payments(login, m["id"])
            if n:
                out[login] = n
    return out


# --------------------------------------------------------------------------- #
# Бизнес-менеджеры
# --------------------------------------------------------------------------- #
def list_businesses(since: str = "", until: str = "") -> list[dict[str, Any]]:
    """BM со счётчиком кабинетов и открутом за период.

    BM — то, чем банят пачками: кабинеты внутри одного умирают вместе.
    Поэтому рядом со статусом верификации сразу видно, сколько кабинетов
    и денег стоит за каждым.
    """
    with connect() as conn:
        base = rows(conn.execute(_q("SELECT * FROM businesses ORDER BY name")))
        counts = {r["bm_id"]: r for r in rows(conn.execute(_q("""
            SELECT bm_id,
                   COUNT(*) AS accounts,
                   SUM(CASE WHEN account_status = 1 THEN 0 ELSE 1 END) AS broken
              FROM ad_accounts WHERE bm_id IS NOT NULL AND bm_id <> ''
             GROUP BY bm_id
        """)))}
        spend: dict[str, Decimal] = {}
        if since and until:
            spend = {r["bm_id"]: dec(r["spend"]) for r in rows(conn.execute(_q("""
                SELECT a.bm_id, COALESCE(SUM(s.spend), 0) AS spend
                  FROM ad_accounts a
                  JOIN spend_daily s ON s.act_id = a.act_id
                 WHERE s.date >= ? AND s.date <= ?
                   AND a.bm_id IS NOT NULL AND a.bm_id <> ''
                 GROUP BY a.bm_id
            """), (since, until)))}
        # Владелец BM. Прямой связи BM→маркетолог нет, но у байера свои BM
        # со своими кабинетами, поэтому владельца BM определяем по владельцам
        # его кабинетов: формальный (ad_account_owner), а нет — по доступу
        # личики (ad_account_access → identities). Берём преобладающего.
        own = {}
        for r in rows(conn.execute(_q("""
            SELECT a.bm_id,
                   o.marketer_id  AS owner_mid,
                   i.marketer_id  AS access_mid
              FROM ad_accounts a
              LEFT JOIN ad_account_owner o
                     ON o.act_id = a.act_id AND o.until IS NULL
              LEFT JOIN ad_account_access x ON x.act_id = a.act_id
              LEFT JOIN identities i ON i.id = x.identity_id
             WHERE a.bm_id IS NOT NULL AND a.bm_id <> ''
        """))):
            mid = r.get("owner_mid") or r.get("access_mid")
            if not mid:
                continue
            tally = own.setdefault(r["bm_id"], {})
            tally[mid] = tally.get(mid, 0) + 1
        mnames = {m["id"]: m["name"] for m in
                  rows(conn.execute(_q("SELECT id, name FROM marketers")))}
    winner = {bm: max(t.items(), key=lambda kv: kv[1])[0]
              for bm, t in own.items() if t}
    for b in base:
        c = counts.get(b["bm_id"]) or {}
        b["accounts"] = int(c.get("accounts") or 0)
        b["broken"] = int(c.get("broken") or 0)
        b["spend"] = spend.get(b["bm_id"], ZERO)
        mid = winner.get(b["bm_id"], "")
        b["owner_marketer_id"] = mid
        b["owner_name"] = mnames.get(mid, "") if mid else ""
    return base


def business_accounts(bm_id: str) -> list[dict[str, Any]]:
    """Кабинеты внутри BM — чтобы из карточки BM проваливаться в кабинет."""
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT act_id, name, account_status, status_label, disable_label,
                   currency, funding_last4, balance, daily_spend_limit
              FROM ad_accounts WHERE bm_id = ? ORDER BY name
        """), (bm_id,)))


# --------------------------------------------------------------------------- #
# Списания FB — деньги, а не оценка по показам
# --------------------------------------------------------------------------- #
def account_transactions(act_id: str, since: str = "", until: str = "",
                         limit: int = 200) -> list[dict[str, Any]]:
    """Фактические списания кабинета.

    Insights и транзакции расходятся законно: FB списывает по порогу, а не
    в момент открута. Поэтому это отдельная таблица на карточке, а не
    «уточнение» спенда.
    """
    sql = "SELECT * FROM fb_transactions WHERE act_id = ?"
    params: list[Any] = [act_id]
    if since:
        sql += " AND time >= ?"
        params.append(since)
    if until:
        sql += " AND time <= ?"
        params.append(until + "T23:59:59")
    sql += " ORDER BY time DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        return rows(conn.execute(_q(sql), tuple(params)))


def account_transaction_totals(act_id: str, since: str = "", until: str = ""
                               ) -> dict[str, Any]:
    """Сколько FB предъявил к оплате за период и сколько из этого НДС."""
    sql = ("SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount, "
           "COALESCE(SUM(tax_amount), 0) AS tax, MAX(currency) AS currency "
           "FROM fb_transactions WHERE act_id = ?")
    params: list[Any] = [act_id]
    if since:
        sql += " AND time >= ?"
        params.append(since)
    if until:
        sql += " AND time <= ?"
        params.append(until + "T23:59:59")
    with connect() as conn:
        found = rows(conn.execute(_q(sql), tuple(params)))
    return dict(found[0]) if found else {"count": 0, "amount": ZERO,
                                         "tax": ZERO, "currency": ""}


# --------------------------------------------------------------------------- #
# Платежи с карт
# --------------------------------------------------------------------------- #
def fb_billing_total(since: str, until: str,
                     marketer_id: str = "") -> dict[str, Any]:
    """Сколько FB списал по кабинетам за период — итог, без разбивки.

    Пара к платежам по картам: карты пополняют кабинеты, кабинеты списывают.
    Байеру важны ровно две цифры рядом — сколько ушло с карт и сколько FB
    предъявил, — а не список отдельных списаний.

    По маркетологу — через EXISTS, а не JOIN: у кабинета бывает несколько
    записей о владении, и джойн размножил бы каждое списание по их числу.
    """
    where = ["t.time >= ?", "t.time <= ?"]
    params: list[Any] = [since, until + "T23:59:59"]
    if marketer_id:
        where.append("EXISTS (SELECT 1 FROM ad_account_owner o"
                     " WHERE o.act_id = t.act_id AND o.marketer_id = ?)")
        params.append(marketer_id)
    with connect() as conn:
        found = rows(conn.execute(_q(f"""
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(t.amount), 0) AS amount,
                   COALESCE(SUM(t.tax_amount), 0) AS tax,
                   MAX(t.currency) AS currency
              FROM fb_transactions t
             WHERE {' AND '.join(where)}
        """), tuple(params)))
    return dict(found[0]) if found else {"count": 0, "amount": ZERO,
                                         "tax": ZERO, "currency": ""}


def _payments_where(since: str, until: str, marketer_id: str = "",
                    team: str = "", card_id: str = "", prefix: str = ""
                    ) -> tuple[str, tuple]:
    """WHERE для card_payments. `prefix` — алиас таблицы с точкой, если есть."""
    clauses = [f"{prefix}date >= ?", f"{prefix}date <= ?"]
    params: list[Any] = [since, until + "T23:59:59"]
    if marketer_id:
        clauses.append(f"{prefix}marketer_id = ?")
        params.append(marketer_id)
    if team:
        clauses.append(f"{prefix}team = ?")
        params.append(team)
    if card_id:
        clauses.append(f"{prefix}card_id = ?")
        params.append(card_id)
    return "WHERE " + " AND ".join(clauses), tuple(params)


def card_payment_totals(since: str, until: str, marketer_id: str = "",
                        team: str = "", card_id: str = "") -> dict[str, Any]:
    """Итоги по платежам, разложенные на то, что складывать вместе нельзя.

    Проведённые, комиссии и отклонённые — три разные величины. Сумма
    проведённых сравнивается со спендом FB; комиссия за отклонённый платёж —
    реальный расход, но не рекламный; сам отклонённый платёж деньгами не
    является вовсе. Одной цифрой это показывать нельзя.
    """
    where, params = _payments_where(since, until, marketer_id, team, card_id)
    with connect() as conn:
        found = rows(conn.execute(_q(f"""
            SELECT COUNT(*) AS payments,
                   COUNT(DISTINCT card_id) AS cards,
                   COUNT(DISTINCT user_email) AS people,
                   SUM(CASE WHEN LOWER(state) IN ('settled','pending')
                            THEN amount ELSE 0 END) AS charged,
                   SUM(CASE WHEN LOWER(state) = 'settled'
                            THEN amount ELSE 0 END) AS settled,
                   SUM(CASE WHEN LOWER(state) = 'pending'
                            THEN amount ELSE 0 END) AS pending,
                   SUM(CASE WHEN LOWER(state) NOT IN ('settled','pending')
                            THEN amount ELSE 0 END) AS declined_amount,
                   SUM(CASE WHEN LOWER(state) NOT IN ('settled','pending')
                            THEN 1 ELSE 0 END) AS declines,
                   SUM(CASE WHEN is_micro = 1 THEN 1 ELSE 0 END) AS micro,
                   COALESCE(SUM(fee), 0) AS fees,
                   MAX(currency) AS currency
              FROM card_payments {where}
        """), params))
    return dict(found[0]) if found else {}


def card_payments_page(since: str, until: str, marketer_id: str = "",
                       team: str = "", card_id: str = "", state: str = "",
                       page: int = 0, size: int = 50) -> dict[str, Any]:
    """Страница платежей. Пагинация обязательна: строк пятнадцать тысяч."""
    where, params = _payments_where(since, until, marketer_id, team, card_id,
                                    prefix="p.")
    if state == "declined":
        where += " AND LOWER(p.state) NOT IN ('settled','pending')"
    elif state in ("settled", "pending"):
        where += " AND LOWER(p.state) = ?"
        params = params + (state,)
    size, page = max(1, min(int(size), 200)), max(0, int(page))
    with connect() as conn:
        total = rows(conn.execute(_q(
            f"SELECT COUNT(*) AS n FROM card_payments p {where}"),
            params))[0]["n"]
        found = rows(conn.execute(_q(f"""
            SELECT p.payment_id, p.card_id, p.card_last4, p.card_bin,
                   p.card_title, p.user_email, p.user_name, p.team,
                   p.merchant, p.descriptor, p.state, p.decline, p.is_micro,
                   p.currency, p.amount, p.fee, p.date, p.date_final,
                   m.name AS marketer_name
              FROM card_payments p
              LEFT JOIN marketers m ON m.id = p.marketer_id
              {where}
             ORDER BY p.date DESC LIMIT ? OFFSET ?
        """), params + (size, page * size)))
    return {"total": int(total), "page": page, "size": size,
            "pages": (int(total) + size - 1) // size, "payments": found}


def card_payment_filters() -> dict[str, Any]:
    """Значения для фильтров — только те команды и карты, что реально есть."""
    with connect() as conn:
        teams = [r["team"] for r in rows(conn.execute(_q(
            "SELECT DISTINCT team FROM card_payments "
            "WHERE team <> '' ORDER BY team")))]
        cards = rows(conn.execute(_q("""
            SELECT card_id, MAX(card_last4) AS last4, MAX(card_title) AS title,
                   MAX(card_bin) AS bin, COUNT(*) AS payments
              FROM card_payments WHERE card_id <> ''
             GROUP BY card_id ORDER BY payments DESC LIMIT 300
        """)))
    return {"teams": teams, "cards": cards}


def card_payments_by_marketer(since: str, until: str) -> list[dict[str, Any]]:
    """Разрез по людям: кто сколько прожёг картами и сколько раз отказ.

    Неприсвоенные платежи (`marketer_id` пуст) не выбрасываются, а собираются
    в отдельную строку — иначе расхождение с общим итогом выглядело бы
    необъяснимым.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT COALESCE(m.name, '— не привязан') AS name,
                   p.marketer_id,
                   MAX(p.user_email) AS user_email,
                   -- Сколько разных адресов попало в строку. У непривязанных
                   -- их много, и показывать один как «его почту» — врать.
                   COUNT(DISTINCT p.user_email) AS emails,
                   MAX(p.team) AS team,
                   COUNT(*) AS payments,
                   COUNT(DISTINCT p.card_id) AS cards,
                   SUM(CASE WHEN LOWER(p.state) IN ('settled','pending')
                            THEN p.amount ELSE 0 END) AS charged,
                   SUM(CASE WHEN LOWER(p.state) NOT IN ('settled','pending')
                            THEN 1 ELSE 0 END) AS declines,
                   COALESCE(SUM(p.fee), 0) AS fees,
                   MAX(p.currency) AS currency
              FROM card_payments p
              LEFT JOIN marketers m ON m.id = p.marketer_id
             WHERE p.date >= ? AND p.date <= ?
             GROUP BY p.marketer_id, m.name
             ORDER BY charged DESC
        """), (since, until + "T23:59:59")))


# --------------------------------------------------------------------------- #
# Контроль: доступы, журнал изменений, права токена, пиксели
# --------------------------------------------------------------------------- #
def replace_account_users(act_id: str, users: list[dict[str, Any]]) -> int:
    """Переписать список тех, кто имеет доступ к кабинету.

    Именно переписать, а не дописать: исчезнувший из списка человек — это
    отозванный доступ, и он должен исчезнуть и у нас. `first_seen_at`
    при этом сохраняется — «когда появился» полезнее, чем «когда пересобрали».
    """
    stamp = now()
    with connect() as conn:
        seen = {r["user_id"]: r["first_seen_at"] for r in rows(conn.execute(
            _q("SELECT user_id, first_seen_at FROM account_users WHERE act_id = ?"),
            (act_id,)))}
        conn.execute(_q("DELETE FROM account_users WHERE act_id = ?"), (act_id,))
        for u in users:
            conn.execute(_q(
                "INSERT INTO account_users (act_id, user_id, name, tasks,"
                " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)"),
                (act_id, u["user_id"], u.get("name", ""), u.get("tasks", ""),
                 seen.get(u["user_id"], stamp), stamp))
    return len(users)


def account_users(act_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM account_users WHERE act_id = ? ORDER BY name"),
            (act_id,)))


def upsert_activity(a: dict[str, Any]) -> None:
    """Событие журнала. Ключ собран из времени, типа и участников —
    у события нет своего id, а повторный сбор не должен его задваивать."""
    fields = ("id", "act_id", "event_time", "event_type", "event_label",
              "actor_id", "actor_name", "object_id", "object_name", "extra_json")
    values = [a.get(f) for f in fields]
    updates = ", ".join(f"{f} = ?" for f in fields[1:])
    with connect() as conn:
        conn.execute(_q(
            f"INSERT INTO account_activities ({', '.join(fields)}, updated_at)"
            f" VALUES ({', '.join(['?'] * len(fields))}, ?)"
            f" ON CONFLICT (id) DO UPDATE SET {updates}, updated_at = ?"),
            (*values, now(), *values[1:], now()))


def account_activities(act_id: str = "", since: str = "", limit: int = 100
                       ) -> list[dict[str, Any]]:
    sql = "SELECT * FROM account_activities"
    where, params = [], []
    if act_id:
        where.append("act_id = ?")
        params.append(act_id)
    if since:
        where.append("event_time >= ?")
        params.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY event_time DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        return rows(conn.execute(_q(sql), tuple(params)))


def replace_permissions(identity_id: str, perms: list[dict[str, Any]]) -> int:
    """Права токена. Переписываются целиком: отозванное право должно
    исчезнуть, иначе список врёт ровно в тот момент, когда он нужен."""
    stamp = now()
    with connect() as conn:
        conn.execute(_q("DELETE FROM token_permissions WHERE identity_id = ?"),
                     (identity_id,))
        for p in perms:
            conn.execute(_q(
                "INSERT INTO token_permissions (identity_id, permission,"
                " status, checked_at) VALUES (?, ?, ?, ?)"),
                (identity_id, p["permission"], p.get("status", ""), stamp))
    return len(perms)


def identity_permissions(identity_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM token_permissions WHERE identity_id = ?"
            " ORDER BY permission"), (identity_id,)))


# Права, без которых часть системы не работает. Список короткий намеренно:
# сюда попадает только то, на что мы реально опираемся.
NEEDED_PERMISSIONS = {
    "ads_read": "чтение статистики — без него сбор пуст",
    "ads_management": "статусы и лимиты кабинетов",
    "business_management": "бизнес-менеджеры и их кабинеты",
    "pages_show_list": "список страниц — первое звено к комментариям",
    "pages_read_engagement": "чтение комментариев",
    "pages_manage_engagement": "скрытие и удаление комментариев",
}


def missing_permissions(identity_id: str) -> list[tuple[str, str]]:
    """Каких нужных прав у токена нет. Пустой ответ у непроверенного токена
    отличается от «всё на месте» — поэтому None-случай зовущий разбирает сам."""
    have = {p["permission"] for p in identity_permissions(identity_id)
            if p.get("status") == "granted"}
    return [(k, v) for k, v in NEEDED_PERMISSIONS.items() if k not in have]


def upsert_pixel(p: dict[str, Any]) -> None:
    """Пиксель. Один и тот же приходит из двух мест — от кабинета
    (`{act}/adspixels`, там нет BM) и от бизнеса (`{bm}/owned_pixels`,
    там нет кабинета). Поэтому пустое поле НЕ затирает заполненное:
    иначе два источника по очереди стирали бы находки друг друга.
    """
    act_id = str(p.get("act_id") or "")
    bm_id = str(p.get("bm_id") or "")
    with connect() as conn:
        conn.execute(_q(
            "INSERT INTO pixels (pixel_id, act_id, bm_id, name, last_fired_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (pixel_id) DO UPDATE SET"
            " act_id = CASE WHEN ? = '' THEN pixels.act_id ELSE ? END,"
            " bm_id = CASE WHEN ? = '' THEN pixels.bm_id ELSE ? END,"
            " name = ?, last_fired_at = ?, updated_at = ?"),
            (p["pixel_id"], act_id, bm_id, p.get("name", ""),
             p.get("last_fired_at"), now(),
             act_id, act_id, bm_id, bm_id,
             p.get("name", ""), p.get("last_fired_at"), now()))


def account_pixels(act_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM pixels WHERE act_id = ? ORDER BY name"), (act_id,)))


def set_business_credit(bm_id: str, credit: dict[str, Any]) -> None:
    if not credit:
        return
    with connect() as conn:
        conn.execute(_q("UPDATE businesses SET credit_available = ?,"
                        " credit_currency = ? WHERE bm_id = ?"),
                     (credit.get("credit_available"),
                      credit.get("credit_currency", ""), bm_id))


# --------------------------------------------------------------------------- #
# Контроль на уровне BM: люди, страницы, пиксели бизнеса
# --------------------------------------------------------------------------- #
def replace_business_users(bm_id: str, users: list[dict[str, Any]]) -> int:
    """Переписать доступ к BM целиком.

    Как и у кабинета: исчезнувший из ответа человек — это отозванный
    доступ, и дописывание вместо замены сделало бы список вечно растущим
    и вечно неверным. `first_seen_at` переживает пересбор — «когда
    появился» отвечает на вопрос «а его добавляли при мне?».
    """
    stamp = now()
    with connect() as conn:
        seen = {r["user_id"]: r["first_seen_at"] for r in rows(conn.execute(
            _q("SELECT user_id, first_seen_at FROM business_users WHERE bm_id = ?"),
            (bm_id,)))}
        conn.execute(_q("DELETE FROM business_users WHERE bm_id = ?"), (bm_id,))
        for u in users:
            conn.execute(_q(
                "INSERT INTO business_users (bm_id, user_id, kind, name, email,"
                " role, title, two_factor, expires_at, first_seen_at,"
                " last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
                (bm_id, u["user_id"], u.get("kind", ""), u.get("name", ""),
                 u.get("email", ""), u.get("role", ""), u.get("title", ""),
                 u.get("two_factor", ""), u.get("expires_at", ""),
                 seen.get(u["user_id"], stamp), stamp))
    return len(users)


def business_users(bm_id: str) -> list[dict[str, Any]]:
    """Люди BM. Приглашения — первыми: доступ, которого ещё нет, но который
    появится сам, важнее уже известного."""
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM business_users WHERE bm_id = ?"
            " ORDER BY CASE kind WHEN 'pending' THEN 0 WHEN 'member' THEN 1"
            " ELSE 2 END, name, email"), (bm_id,)))


def replace_business_pages(bm_id: str, pages: list[dict[str, Any]]) -> int:
    """Страницы бизнеса целиком. Отобранная клиентская страница должна
    исчезнуть из списка — иначе он врёт ровно там, где встала реклама."""
    stamp = now()
    with connect() as conn:
        conn.execute(_q("DELETE FROM business_pages WHERE bm_id = ?"), (bm_id,))
        for p in pages:
            conn.execute(_q(
                "INSERT INTO business_pages (page_id, bm_id, kind, name, link,"
                " category, verification_status, fan_count, is_published,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
                (p["page_id"], bm_id, p.get("kind", ""), p.get("name", ""),
                 p.get("link", ""), p.get("category", ""),
                 p.get("verification_status", ""), int(p.get("fan_count") or 0),
                 int(p.get("is_published", 1)), stamp))
    return len(pages)


def business_pages(bm_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM business_pages WHERE bm_id = ?"
            " ORDER BY kind, name"), (bm_id,)))


def business_pixels(bm_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM pixels WHERE bm_id = ? ORDER BY name"), (bm_id,)))


# --------------------------------------------------------------------------- #
# Автоправила
# --------------------------------------------------------------------------- #
def upsert_ad_rule(r: dict[str, Any]) -> None:
    """Автоправило кабинета. Обновляется, а не переписывается пачкой:
    удалённое правило редко, а история `created_time` полезна."""
    fields = ("rule_id", "act_id", "name", "status", "action", "condition_text",
              "schedule", "created_time", "updated_time", "spec_json")
    values = [r.get(f) for f in fields]
    updates = ", ".join(f"{f} = ?" for f in fields[1:])
    with connect() as conn:
        conn.execute(_q(
            f"INSERT INTO ad_rules ({', '.join(fields)}, updated_at)"
            f" VALUES ({', '.join(['?'] * len(fields))}, ?)"
            f" ON CONFLICT (rule_id) DO UPDATE SET {updates}, updated_at = ?"),
            (*values, now(), *values[1:], now()))


def account_rules(act_id: str) -> list[dict[str, Any]]:
    """Правила кабинета. Активные наверху: выключенное правило ничего
    не делает, а включённое объясняет чужие изменения бюджета."""
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT * FROM ad_rules WHERE act_id = ?"
            " ORDER BY CASE WHEN status = 'ENABLED' THEN 0 ELSE 1 END, name"),
            (act_id,)))


# --------------------------------------------------------------------------- #
# Управление: журнал наших действий и данные для карточки кампании
# --------------------------------------------------------------------------- #
def log_action(row: dict[str, Any]) -> None:
    """Записать, что мы изменили у Meta.

    Пишется и успех, и отказ. Отказ важнее: «я нажал, ничего не произошло»
    иначе не отличить от «я не нажимал», а разница определяет, идти ли
    смотреть в Ads Manager. `before_val` хранится всегда — Meta держит
    только текущее значение, и восстановить прежнее потом будет неоткуда.
    """
    fields = ("actor_id", "actor_name", "actor_role", "act_id", "target_kind",
              "target_id", "target_name", "op", "before_val", "after_val",
              "status", "error")
    with connect() as conn:
        conn.execute(_q(
            f"INSERT INTO control_actions (at, {', '.join(fields)})"
            f" VALUES ({', '.join(['?'] * (len(fields) + 1))})"),
            (now(), *[str(row.get(f) or "") for f in fields]))


def account_actions(act_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Что делали из Admetrics. Пустой `act_id` — по всем кабинетам."""
    sql = "SELECT * FROM control_actions"
    params: list[Any] = []
    if act_id:
        sql += " WHERE act_id = ?"
        params.append(act_id)
    sql += " ORDER BY at DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        return rows(conn.execute(_q(sql), tuple(params)))


def campaign_daily(act_id: str, campaign_id: str, since: str, until: str
                   ) -> list[dict[str, Any]]:
    """Открут одной кампании по дням.

    Нужен, чтобы график в карточке кабинета переключался вместе с выбором
    кампании: «упало» по кабинету и «упало» по конкретной кампании — разные
    утверждения, и второе показывает, какая именно связка встала.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT date,
                   COALESCE(SUM(spend), 0)       AS spend,
                   COALESCE(SUM(impressions), 0) AS impressions,
                   COALESCE(SUM(clicks), 0)      AS clicks,
                   COALESCE(SUM(leads), 0)       AS leads
              FROM spend_campaign_daily
             WHERE act_id = ? AND campaign_id = ? AND date >= ? AND date <= ?
             GROUP BY date ORDER BY date
        """), (act_id, campaign_id, since, until)))


def campaign_adsets(act_id: str, campaign_id: str, since: str, until: str
                    ) -> list[dict[str, Any]]:
    """Адсеты одной кампании со спендом за период.

    Кампания — это бюджет и цель, а решения принимаются на уровне адсета:
    остановить надо не всю кампанию, а ту аудиторию, которая не идёт.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT a.*, COALESCE(s.spend, 0) AS spend,
                   COALESCE(s.impressions, 0) AS impressions,
                   COALESCE(s.clicks, 0) AS clicks
              FROM adsets a
              -- Спенд адсета собирается через его объявления: своего
              -- adset_id в `ad_daily` нет, связь живёт в реестре `ads`.
              LEFT JOIN (SELECT x.adset_id,
                                SUM(d.spend) AS spend,
                                SUM(d.impressions) AS impressions,
                                SUM(d.clicks) AS clicks
                           FROM ad_daily d
                           JOIN ads x ON x.ad_id = d.ad_id
                          WHERE d.act_id = ? AND d.date >= ? AND d.date <= ?
                          GROUP BY x.adset_id) s ON s.adset_id = a.adset_id
             WHERE a.act_id = ? AND a.campaign_id = ?
             ORDER BY spend DESC, a.name
        """), (act_id, since, until, act_id, campaign_id)))


def adset_ads(act_id: str, adset_id: str, since: str, until: str
              ) -> list[dict[str, Any]]:
    """Все объявления адсета — со спендом, креативом и связью с трекером.

    Именно ВСЕ, а не только те, у кого есть открут: `top_ads` строится от
    `ad_daily` и потому показывает лишь потратившие. В дереве это было бы
    ложью — остановленное объявление никуда не делось, и человек ищет
    глазами именно его.
    """
    with connect() as conn:
        return rows(conn.execute(_q("""
            SELECT a.ad_id, a.name, a.effective_status, a.issues_json,
                   COALESCE(SUM(d.spend), 0)       AS spend,
                   COALESCE(SUM(d.impressions), 0) AS impressions,
                   COALESCE(SUM(d.clicks), 0)      AS clicks,
                   COALESCE(SUM(d.link_clicks), 0) AS link_clicks,
                   COALESCE(SUM(d.leads), 0)       AS leads,
                   MAX(d.currency) AS currency,
                   MAX(m.title) AS title, MAX(m.body) AS body,
                   MAX(m.thumb_url) AS thumb_url,
                   MAX(m.preview_url) AS preview_url,
                   MAX(m.link_url) AS link_url,
                   MAX(m.permalink) AS permalink,
                   MAX(m.object_type) AS object_type,
                   MAX(ak.kt_id) AS kt_id,
                   MAX(ak.link) AS kt_link,
                   MAX(ak.matched_by) AS kt_matched_by,
                   MAX(k.name) AS kt_name
              FROM ads a
              LEFT JOIN ads_meta m ON m.ad_id = a.ad_id
              LEFT JOIN ad_daily d ON d.ad_id = a.ad_id
                   AND d.date >= ? AND d.date <= ?
              LEFT JOIN ad_keitaro ak ON ak.ad_id = a.ad_id
              LEFT JOIN keitaro_campaigns k ON k.kt_id = ak.kt_id
             WHERE a.act_id = ? AND a.adset_id = ?
             GROUP BY a.ad_id
             ORDER BY spend DESC, a.name
        """), (since, until, act_id, adset_id)))


def set_campaign_local(campaign_id: str, state: dict[str, Any]) -> None:
    """Обновить нашу копию кампании после изменения у Meta.

    Иначе экран показывал бы прежнее значение до следующего прогона сбора,
    и человек, только что нажавший «остановить», увидел бы кампанию
    работающей — и нажал бы ещё раз.
    """
    with connect() as conn:
        conn.execute(_q(
            "UPDATE campaigns SET status = ?, effective_status = ?,"
            " daily_budget = ?, lifetime_budget = ?, budget_remaining = ?,"
            " updated_at = ? WHERE campaign_id = ?"),
            (state.get("status", ""), state.get("effective_status", ""),
             state.get("daily_budget"), state.get("lifetime_budget"),
             state.get("budget_remaining"), now(), campaign_id))


def set_adset_local(adset_id: str, state: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(_q(
            "UPDATE adsets SET status = ?, effective_status = ?,"
            " daily_budget = ?, lifetime_budget = ?, updated_at = ?"
            " WHERE adset_id = ?"),
            (state.get("status", ""), state.get("effective_status", ""),
             state.get("daily_budget"), state.get("lifetime_budget"),
             now(), adset_id))


def identity_for_account(act_id: str) -> dict[str, Any] | None:
    """Личика с СЕКРЕТОМ, через которую можно писать в кабинет.

    `account_access` отдаёт статус без секрета — для показа этого хватает,
    для запроса к Meta нет. Отдельная функция, чтобы секрет не попадал
    в ответы API случайно: он берётся только там, где действительно нужен.
    """
    sql = """
        SELECT i.id, i.secret_encrypted, i.proxy_encrypted, i.platform,
               i.status, i.session_name, i.label, i.marketer_id
          FROM ad_account_access x
          JOIN identities i ON i.id = x.identity_id
         WHERE x.act_id = ?{extra}
         ORDER BY i.last_checked_at DESC LIMIT 1
    """
    with connect() as conn:
        found = rows(conn.execute(_q(sql.format(extra=" AND i.status = 'ok'")),
                                  (act_id,)))
        if not found:
            # Живой не нашлось — отдаём любую, чтобы вызывающий объяснил,
            # ПОЧЕМУ не вышло, вместо безликого «доступа нет».
            found = rows(conn.execute(_q(sql.format(extra="")), (act_id,)))
    if not found:
        return None
    row = found[0]
    # Расшифровка может не выйти — нет ключа, ключ сменили. Это не повод
    # уронить запрос: вызывающий увидит пустой секрет и скажет человеку,
    # что токена нет, вместо стектрейса в браузере.
    try:
        row["secret"] = decrypt(row.pop("secret_encrypted"))
        row["proxy"] = decrypt(row.pop("proxy_encrypted"))
    except Exception:  # noqa: BLE001
        row.pop("secret_encrypted", None)
        row.pop("proxy_encrypted", None)
        row["secret"] = ""
        row["proxy"] = ""
    return row


def campaign_row(campaign_id: str) -> dict[str, Any] | None:
    """Кампания как она у нас записана. Нужна до изменения — чтобы
    в журнале осталось «что было»: Meta хранит только «что стало»."""
    if not campaign_id:
        return None
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT * FROM campaigns WHERE campaign_id = ?"), (campaign_id,)))
    return found[0] if found else None


def adset_row(adset_id: str) -> dict[str, Any] | None:
    if not adset_id:
        return None
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT * FROM adsets WHERE adset_id = ?"), (adset_id,)))
    return found[0] if found else None


def identity_with_secret(identity_id: str) -> dict[str, Any] | None:
    """Одна личика с расшифрованными секретом и прокси.

    Отдельно от `list_identities`, который секрет не отдаёт намеренно:
    расшифрованный токен должен появляться только там, где им сейчас
    будут пользоваться, а не в каждом ответе списка.
    """
    if not identity_id:
        return None
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT * FROM identities WHERE id = ?"), (identity_id,)))
    if not found:
        return None
    row = found[0]
    try:
        row["secret"] = decrypt(row.pop("secret_encrypted", None))
        row["proxy"] = decrypt(row.pop("proxy_encrypted", None))
    except Exception:  # noqa: BLE001 — нет ключа или ключ сменили
        row.pop("secret_encrypted", None)
        row.pop("proxy_encrypted", None)
        row["secret"] = ""
        row["proxy"] = ""
    return row


# --------------------------------------------------------------------------- #
# Ключи внешнего API
# --------------------------------------------------------------------------- #
def _key_hash(raw: str) -> str:
    """Хеш ключа. SHA-256 без соли — намеренно.

    Соль нужна против перебора слабых секретов; здесь секрет — 32 случайных
    байта, перебирать нечего. Зато без соли ключ ищется одним индексным
    запросом, а не перебором всех строк с проверкой каждой.
    """
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_key(name: str, marketer_id: str = "", scope: str = "read",
                   created_by: str = "") -> tuple[str, dict[str, Any]]:
    """Выпустить ключ. Возвращает (сам ключ, строку реестра).

    Сам ключ существует ровно здесь и уходит вызывающему один раз — в базе
    остаётся только хеш и первые символы для опознания в списке.
    """
    import secrets as _secrets
    raw = "adm_" + _secrets.token_urlsafe(32)
    row = {"id": new_id(), "name": name[:64], "key_hash": _key_hash(raw),
           "prefix": raw[:12], "marketer_id": marketer_id or "",
           "scope": scope, "created_at": now(), "created_by": created_by or ""}
    with connect() as conn:
        conn.execute(_q(
            "INSERT INTO api_keys (id, name, key_hash, prefix, marketer_id,"
            " scope, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"),
            tuple(row[k] for k in ("id", "name", "key_hash", "prefix",
                                   "marketer_id", "scope", "created_at",
                                   "created_by")))
    return raw, row


def api_key_by_secret(raw: str, ip: str = "") -> dict[str, Any] | None:
    """Найти живой ключ по предъявленному секрету и отметить использование.

    Отметка нужна не для статистики: по «последний раз использован» видно,
    какой ключ можно отозвать без звонка, а какой действительно работает.
    """
    if not raw or not raw.startswith("adm_"):
        return None
    with connect() as conn:
        found = rows(conn.execute(_q(
            "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL"),
            (_key_hash(raw),)))
        if not found:
            return None
        conn.execute(_q(
            "UPDATE api_keys SET last_used_at = ?, last_ip = ?,"
            " calls = calls + 1 WHERE id = ?"),
            (now(), (ip or "")[:45], found[0]["id"]))
    return found[0]


def list_api_keys() -> list[dict[str, Any]]:
    """Реестр ключей. Секрета здесь нет и быть не может — только опознание."""
    with connect() as conn:
        return rows(conn.execute(_q(
            "SELECT id, name, prefix, marketer_id, scope, created_at,"
            " created_by, last_used_at, last_ip, calls, revoked_at"
            " FROM api_keys ORDER BY revoked_at IS NULL DESC, created_at DESC")))


def revoke_api_key(key_id: str) -> bool:
    """Отозвать ключ. Не удаляем: строка отвечает на «кто и когда им ходил»
    и после отзыва — удалить её значит стереть историю доступа."""
    with connect() as conn:
        cur = conn.execute(_q(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"),
            (now(), key_id))
        return bool(getattr(cur, "rowcount", 0))

# --------------------------------------------------------------------------- #
# Выдача наружу: плоские строки для внешнего API
# --------------------------------------------------------------------------- #
# Отдельно от функций портала намеренно. Портальные считают производные
# метрики, сортируют под экран и возвращают то, что нужно карточке; их форма
# меняется вместе с вёрсткой. Здесь — сырые факты в стабильной форме, которую
# можно обещать наружу и не переделывать при каждой правке интерфейса.

def _api_owner_filter(marketer_id: str) -> tuple[str, tuple[Any, ...]]:
    """Ограничение по владельцу — для ключей, выпущенных на человека.

    Владение интервальное, поэтому сравниваем с периодом, а не с «сегодня»:
    кабинет, переданный вчера, должен остаться в выгрузке за позавчера.
    """
    if not marketer_id:
        return "", ()
    return (" AND EXISTS (SELECT 1 FROM ad_account_owner o"
            " WHERE o.act_id = t.act_id AND o.marketer_id = ?"
            " AND o.since <= t.date AND (o.until IS NULL OR o.until >= t.date))",
            (marketer_id,))


def api_spend_rows(level: str, since: str, until: str,
                   act_ids: list[str] | None, marketer_id: str,
                   limit: int, offset: int) -> list[dict[str, Any]]:
    table = "spend_campaign_daily" if level == "campaign" else "spend_daily"
    cols = ("t.date, t.act_id, t.campaign_id, t.campaign_name, t.currency,"
            if level == "campaign" else "t.date, t.act_id, t.currency,")
    where, params = ["t.date >= ?", "t.date <= ?"], [since, until]
    if act_ids:
        where.append("t.act_id IN (" + ",".join("?" * len(act_ids)) + ")")
        params += list(act_ids)
    own, own_params = _api_owner_filter(marketer_id)
    sql = (f"SELECT {cols} t.spend, t.impressions, t.clicks, t.link_clicks,"
           f" t.leads, t.purchases, t.revenue FROM {table} t"
           f" WHERE {' AND '.join(where)}{own}"
           f" ORDER BY t.date, t.act_id LIMIT ? OFFSET ?")
    with connect() as conn:
        return rows(conn.execute(_q(sql),
                                 (*params, *own_params, int(limit), int(offset))))


def api_fb_transactions(since: str, until: str, marketer_id: str,
                        limit: int, offset: int) -> list[dict[str, Any]]:
    own, own_params = _api_owner_filter(marketer_id)
    # У транзакций поле времени называется `time`, а не `date` — подменяем
    # его для фильтра по владельцу, иначе SQL не сойдётся.
    own = own.replace("o.since <= t.date", "o.since <= substr(t.time, 1, 10)") \
             .replace("o.until >= t.date", "o.until >= substr(t.time, 1, 10)")
    sql = ("SELECT t.tx_id, t.act_id, t.time, t.amount, t.currency, t.tax_amount,"
           " t.charge_type, t.status, t.payment_option FROM fb_transactions t"
           " WHERE t.time >= ? AND t.time <= ?" + own +
           " ORDER BY t.time DESC LIMIT ? OFFSET ?")
    with connect() as conn:
        return rows(conn.execute(_q(sql), (since, until + "T23:59:59",
                                           *own_params, int(limit), int(offset))))


def api_card_payments(since: str, until: str, marketer_id: str,
                      limit: int, offset: int) -> list[dict[str, Any]]:
    """Платежи с карт. Здесь владение проверяется по самой строке платежа:
    у неё свой `marketer_id`, проставленный при разборе, а кабинета может
    не быть вовсе — карта могла платить за что-то ещё."""
    where, params = ["p.date >= ?", "p.date <= ?"], [since, until + "T23:59:59"]
    if marketer_id:
        where.append("p.marketer_id = ?")
        params.append(marketer_id)
    sql = ("SELECT p.payment_id, p.date, p.date_final, p.amount, p.fee,"
           " p.currency, p.state, p.decline, p.card_last4, p.card_bin,"
           " p.card_title, p.merchant, p.descriptor, p.team, p.marketer_id,"
           " p.user_email FROM card_payments p"
           f" WHERE {' AND '.join(where)}"
           " ORDER BY p.date DESC LIMIT ? OFFSET ?")
    with connect() as conn:
        return rows(conn.execute(_q(sql), (*params, int(limit), int(offset))))
