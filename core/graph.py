"""
Клиент Graph API — общий для разведчика и коллектора.

Собран из рабочей части `probe.py` и дополнен тем, чего не хватало обеим
исходным заготовкам:

  • **чтение usage-заголовков** Meta (`X-Business-Use-Case-Usage` и соседи) —
    единственный честный сигнал «пора притормозить». Без него на масштабе
    команды токены начнут ловить 17/613, а система запишет это как проблему
    с токеном и начнёт врать про здоровье личек;
  • **классификация ошибок** на `AuthError` (личка мертва, нужен реконнект)
    и `TransientError` (переживём, повторим). Дизайн взят из SpendTool, но
    с разбором субкодов 190: смена пароля, чекпойнт и истечение — разные
    сценарии с разными действиями;
  • **batch-запросы** — до 50 подзапросов за один HTTP-вызов. При 20 личках
    по 30 кабинетов это разница между тысячами вызовов и десятками.

Модуль ничего не знает про базу и про бизнес-логику: только HTTP и Meta.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
import sys
import time
from typing import Any, Iterator

import requests

BASE_URL = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"
REQUEST_TIMEOUT = 60
MAX_RETRIES = 4
BATCH_LIMIT = 50

NONEXISTING_FIELD_RE = re.compile(
    r"(nonexisting field|Unsupported get request|does not exist|cannot be loaded)", re.I)

# Транзиентные коды Meta: перегрузка, лимиты, временная недоступность.
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}

# Субкоды 190 — из них видно, что именно случилось с личкой. Разные причины
# требуют разного сообщения маркетологу, иначе алерт бесполезен.
OAUTH_SUBCODES = {
    458: "приложение удалено из аккаунта",
    459: "аккаунт на чекпойнте",
    460: "сменился пароль",
    461: "аккаунт заблокирован Meta",
    463: "токен истёк",
    464: "аккаунт не подтверждён",
    467: "токен отозван",
    490: "аккаунт ограничен",
    492: "нет доступа к объекту",
}

# Если субкода нет, причину часто видно в тексте ошибки FB — вытаскиваем её,
# это точнее общего «доступ отозван или истёк».
OAUTH_MESSAGE_HINTS = [
    ("changed the password", "сменился пароль"),
    ("changed their password", "сменился пароль"),
    ("session has expired", "токен истёк"),
    ("session is invalid", "сессия недействительна"),
    ("logged out", "разлогинен"),
    ("checkpoint", "аккаунт на чекпойнте"),
    ("not a confirmed user", "аккаунт не подтверждён"),
    ("not authorized", "приложение не авторизовано"),
    ("has been disabled", "аккаунт отключён Meta"),
    ("been deleted", "аккаунт удалён"),
]


class GraphError(RuntimeError):
    def __init__(self, message: str, code: Any = None, subcode: Any = None,
                 http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code,
                "subcode": self.subcode, "http_status": self.http_status}


class AuthError(GraphError):
    """Личка мертва: токен отозван, истёк, чекпойнт, сменился пароль.
    Действие — пометить `needs_reconnect` и позвать маркетолога в Telegram."""

    @property
    def human(self) -> str:
        if self.subcode in OAUTH_SUBCODES:
            return OAUTH_SUBCODES[self.subcode]
        low = str(self).lower()
        for needle, reason in OAUTH_MESSAGE_HINTS:
            if needle in low:
                return reason
        return "доступ отозван или истёк"


class TransientError(GraphError):
    """Временная ошибка: лимит, перегрузка, сеть. Действие — повторить позже,
    личку живой считать по-прежнему."""


def classify(code: Any, subcode: Any, message: str,
             http_status: int | None = None) -> GraphError:
    """Ошибка Meta → тип, по которому принимается решение.

    Ключевая тонкость: код 190 в старом коллекторе безусловно означал «токен
    мёртв». На деле 190 приходит и при смене пароля, и при чекпойнте, и при
    истечении — все они требуют реконнекта, но текст маркетологу разный.
    А вот 102 без субкода бывает и транзиентным, поэтому он тоже AuthError,
    но с мягкой формулировкой.
    """
    if code in (190, 102, 467):
        return AuthError(message, code, subcode, http_status)
    if code in TRANSIENT_CODES:
        return TransientError(message, code, subcode, http_status)
    if http_status and http_status >= 500:
        return TransientError(message, code, subcode, http_status)
    return GraphError(message, code, subcode, http_status)


# --------------------------------------------------------------------------- #
# Контейнер FBTOOL
# --------------------------------------------------------------------------- #
def hide_secrets(text: Any) -> str:
    """Убрать токен из текста ошибки.

    Библиотека сетевого клиента вкладывает в исключение весь URL, а токен
    едет в query-параметре — так он утекал в лог и в `collection_tasks`,
    откуда его мог прочитать любой, кому показали журнал сбора.
    """
    return re.sub(r"(access_token=)[^&\s'\")]+", r"\1<скрыт>", str(text))


# Сетевые обломы, которые в сыром виде — простыня из urllib3, а по сути
# три разных поломки с тремя разными действиями. Показывать человеку стек
# вызовов вместо «что чинить» значит не сказать ничего.
_NET_HINTS = [
    # Эта ошибка приходит ДО сети: библиотека не смогла прочитать сам адрес
    # прокси. Без подсказки она читается как «прокси не работает», хотя
    # прокси в этот момент никто и не дёргал.
    (r"Failed to parse|InvalidURL|Invalid URL",
     "адрес прокси не разобрался — скорее всего он записан как "
     "socks5://хост:порт:логин:пароль. Откройте личику, «Сменить прокси» "
     "и сохраните его заново: формат мы приведём сами"),
    (r"Name or service not known|nodename nor servname|Temporary failure in name",
     "не удалось разрешить имя graph.facebook.com"),
    (r"SOCKS|proxy|Proxy",
     "прокси не отвечает или отказал в соединении — проверьте адрес, "
     "порт и логин"),
    (r"timed out|Read timed out|ConnectTimeout",
     "Meta не ответила за отведённое время — сеть или прокси медленные"),
    (r"Connection refused|Network is unreachable|Errno 101|Errno 111",
     "до graph.facebook.com нет сети — на этой машине Meta закрыта, "
     "нужен прокси вне блокировки"),
    (r"certificate|SSL|TLS",
     "не сошёлся сертификат — между нами и Meta кто-то вмешивается "
     "в соединение"),
]


def explain_network(exc: Any, has_proxy: bool | None = None) -> str:
    """Сетевую ошибку — одной понятной фразой, с сырым текстом следом.

    Сырой текст оставляем: по нему разбираются, когда подсказка не подошла.
    Но первым идёт то, что делать.

    `has_proxy` отличает два случая, которые выглядят одинаково, а чинятся
    по-разному: «прокси есть, но схема неверная» и «прокси нет вовсе».
    Раньше сообщение советовало поправить схему даже тогда, когда править
    было нечего, — и человек искал опечатку в строке, которой не существует.
    """
    raw = hide_secrets(exc)
    dns = re.search(r"Name or service not known|nodename nor servname"
                    r"|Temporary failure in name|Failed to resolve", raw)
    if dns:
        if has_proxy is False:
            tail = ("у этой личики не задан прокси, а напрямую с этой машины "
                    "Meta недоступна — пропишите прокси личике")
        elif has_proxy:
            tail = ("прокси задан, но имя разрешает наша машина. Адрес должен "
                    "разрешаться на стороне прокси; попробуйте socks5h:// — с socks5:// запрос падает "
                    "на DNS, не дойдя до прокси")
        else:
            tail = ("если прокси задан, адрес должен начинаться с socks5h://; "
                    "если не задан — напрямую с этой машины Meta недоступна")
        return f"не удалось разрешить имя graph.facebook.com: {tail}. " \
               f"Подробности: {raw[:250]}"
    for pattern, hint in _NET_HINTS:
        if re.search(pattern, raw):
            return f"{hint}. Подробности: {raw[:300]}"
    return raw[:400]


def decode_session_container(value: str) -> dict[str, Any]:
    """Контейнер FBTOOL — base64 от JSON вида {token, cookies[], ua}."""
    compact = "".join(value.split())
    if not compact:
        raise ValueError("пустой session-контейнер")
    compact += "=" * (-len(compact) % 4)
    try:
        payload = json.loads(base64.b64decode(compact, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("session должен быть Base64-кодированным JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("внутри session ожидается JSON-объект")
    if not isinstance(payload.get("token"), str) or not payload["token"].strip():
        raise ValueError("внутри session отсутствует token")
    return payload


# Свежий токен добывается из ЖИВОЙ СЕССИИ, а не хранится снимком. Токен из
# расширения — это токен, который Ads Manager держит в странице и который сам
# перевыпускает каждый час-два из куков c_user/xs. Снимок такого токена
# протухает за пару часов, а сессия живёт месяцами. Поэтому мы не храним
# токен — храним сессию и достаём токен заново перед каждым сбором, ровно то,
# что делает расширение.

# Токен в HTML Ads Manager лежит как "access_token":"EAA…" или accessToken:"EAA…".
# Берём самое длинное совпадение: на странице попадаются и короткие обрезки.
_PAGE_TOKEN_RE = re.compile(r'EAA[0-9A-Za-z]{20,}')

# На странице Ads Manager десятки строк EAA…: токены страниц, DTSG-подобные,
# обрезки. Нужен ровно тот, что подписан accessToken — window.__accessToken
# или "access_token":"…". Якоримся на подпись, а не берём первый попавшийся.
_ANCHORED_TOKEN_RE = re.compile(
    r'access_?token\\?["\']?\s*[:=]\s*\\?["\'](EAA[0-9A-Za-z]{30,})',
    re.I)


def _diag(text: str) -> dict[str, Any]:
    """Как записан токен на странице и сколько разных EAA-строк рядом.

    Нужно, когда токен извлекается, но граф его не принимает: значит взяли
    не ту строку. Токены не печатаем — только длины и подписи вокруг.
    """
    prefixes: dict[str, int] = {}
    for tok in _PAGE_TOKEN_RE.findall(text):
        key = f"{tok[:15]}…(len {len(tok)})"
        prefixes[key] = prefixes.get(key, 0) + 1
    ctx = ""
    m = _ANCHORED_TOKEN_RE.search(text)
    if m:
        start = max(0, m.start() - 40)
        ctx = _redact(text[start:m.start(1)]) + f"«ТОКЕН len {len(m.group(1))}»"
    return {"anchored_context": ctx,
            "eaa_kinds": sorted(prefixes.items(), key=lambda kv: -kv[1])[:8]}


def _extract_token(text: str) -> str:
    """Токен из HTML: сперва подписанный accessToken, иначе самый длинный EAA."""
    m = _ANCHORED_TOKEN_RE.search(text)
    if m:
        return m.group(1)
    bare = _PAGE_TOKEN_RE.findall(text)
    return max(bare, key=len) if bare else ""

# Страницы, которые Ads Manager отдаёт залогиненной сессии с токеном внутри.
# Первая — та, что открыта у человека в браузере; остальные запасные, на
# случай если Meta переставит токен в другой раздел.
_TOKEN_PAGES = (
    "https://adsmanager.facebook.com/adsmanager/manage/campaigns",
    "https://business.facebook.com/adsmanager/manage/campaigns",
    "https://www.facebook.com/adsmanager/manage/campaigns",
)


# Плоский GET без браузерных заголовков Facebook встречает заглушкой:
# редирект, «обновите браузер» или гейт согласия. Настоящую страницу с
# токеном отдают только запросу, неотличимому от навигации браузера.
_NAV_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

_REDACT = re.compile(r"(EAA[0-9A-Za-z]{10,})|(://[^/@\s]+:[^/@\s]+@)"
                     r"|(c_user=\d+)|(xs=[^;\s\x22]+)")


def _redact(text: Any) -> str:
    return _REDACT.sub("…", str(text))


def reinject_token(raw: str, new_token: str) -> str:
    """Заменить токен в контейнере сессии, сохранив куки и UA.

    Токен Ads Manager валиден только в связке со своей сессией: граф
    принимает его лишь вместе с куками c_user/xs. Поэтому обновлённый токен
    возвращается не голым, а вложенным обратно в тот же контейнер — его и
    кладём в базу, его и скармливаем сборщику.
    """
    cont = decode_session_container(raw)
    cont["token"] = new_token
    packed = json.dumps(cont, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(packed).decode("ascii")


def refresh_container(raw: str, proxy: str = "") -> tuple[str, str, str]:
    """Обновить токен в контейнере из живой сессии. (новый_контейнер, прокси, ошибка).

    Возвращает и прокси: если внутри произошёл переход на socks5h (DNS на
    нашей стороне), наружу отдаётся именно рабочий вариант — его сборщик и
    сохранит, чтобы в следующий раз не спотыкаться на том же.

    Голый токен (без кук) обновить нельзя — вернём его как есть с пометкой.
    """
    if not looks_like_container(raw):
        return "", proxy, "не контейнер — обновлять нечего"
    info = token_page_debug(raw, proxy)
    tok = info.get("token") or ""
    if not tok:
        return "", proxy, info.get("error") or "токен не добыт"
    used_proxy = proxy
    if info.get("dns_switched_to"):
        used_proxy = remote_dns_variant(proxy) or proxy
    return reinject_token(raw, tok), used_proxy, ""


def token_page_debug(raw: str, proxy: str = "", act_id: str = "") -> dict[str, Any]:
    """Добыть токен из сессии и вернуть подробности каждой попытки.

    Основная работа refresh_token_from_session; вынесено отдельно, чтобы
    диагностика видела, что именно вернул Facebook, не светя токен и куки.
    """
    info: dict[str, Any] = {"token": "", "error": "", "tries": []}
    if raw.strip().startswith("EAA"):
        info["error"] = ("это голый токен, а не сессия — обновлять нечего, "
                         "переподключите личку контейнером с куками")
        return info
    try:
        session, _stale, meta = build_session(raw, proxy)
    except ValueError as exc:
        info["error"] = f"сессия не разобрана: {exc}"
        return info
    if not meta.get("cookies"):
        info["error"] = ("в контейнере нет куков — из него токен не "
                         "перевыпустить, нужен снимок сессии целиком")
        return info
    session.headers.update(_NAV_HEADERS)

    suffix = f"?act={urllib.parse.quote(act_id)}" if act_id else ""
    for base in _TOKEN_PAGES:
        url = base + suffix
        # Страница Ads Manager — многомегабайтная, и мобильный прокси рвёт её
        # на середине (ChunkedEncodingError). Но токен лежит в самом верху
        # (window.__accessToken, строка ~23), поэтому читаем ПОТОКОМ и
        # обрываем, как только нашли — всю страницу тянуть незачем. Если
        # обрыв всё же случился, ищем токен в том, что успело прийти.
        attempts = []
        got = ""
        for attempt in range(3):
            buf = ""
            status = final = ""
            cut = ""
            try:
                resp = session.get(url, timeout=30, allow_redirects=True,
                                   stream=True)
                status = resp.status_code
                final = resp.url
                if "/login" in final or "login.php" in final:
                    info["error"] = ("сессия истекла — Facebook просит войти "
                                     "заново")
                    resp.close()
                    return info
                for chunk in resp.iter_content(chunk_size=65536,
                                               decode_unicode=True):
                    if not chunk:
                        continue
                    buf += chunk if isinstance(chunk, str) \
                        else chunk.decode("utf-8", "replace")
                    # Ждём ИМЕННО подписанный accessToken, а не первый EAA: на
                    # странице десятки EAA-строк, почти все — не тот токен.
                    hit = _ANCHORED_TOKEN_RE.search(buf)
                    if hit:
                        got = hit.group(1)
                        break
                    if len(buf) > 3_000_000:   # 3 МБ — токен давно был бы выше
                        break
                resp.close()
            except requests.RequestException as exc:
                cut = type(exc).__name__
            # что бы ни пришло — извлекаем подписанный токен из накопленного
            # и снимаем диагностику: как записан токен и какие EAA есть рядом.
            if not got:
                got = _extract_token(buf)
            if buf and "diag" not in info:
                info["diag"] = _diag(buf)
            attempts.append({"try": attempt + 1, "status": status,
                             "final_url": _redact(final),
                             "read": len(buf),
                             "cut": cut,
                             "snippet": _redact(buf[:300].replace("\n", " "))})
            if got:
                break
        info["tries"].append({"url": base, "attempts": attempts})
        if got:
            info["token"] = got
            return info
    # DNS на нашей стороне: то же, что в сборе. Если имя не разрешилось и у
    # прокси есть h/a-вариант — пробуем через него один раз, но только если
    # его ещё не пробовали (иначе бесконечная рекурсия).
    if not info["token"] and not info.get("_dns_retry"):
        joined = " ".join(str(a) for t in info["tries"]
                          for a in t.get("attempts", []))
        alt = remote_dns_variant(proxy)
        if alt and (dns_failed(joined) or dns_failed(info.get("error", ""))
                    or "ConnectionError" in joined):
            alt_info = token_page_debug(raw, alt, act_id)
            alt_info["_dns_retry"] = True
            alt_info["dns_switched_to"] = "socks5h/socks4a"
            return alt_info
    if not info["error"]:
        info["error"] = "ни одна страница не отдала токен"
    return info


def refresh_token_from_session(raw: str, proxy: str = "",
                               act_id: str = "") -> tuple[str, str]:
    """Свежий токен из сессии-контейнера. Возвращает (token, error).

    Запрос ОБЯЗАТЕЛЬНО через прокси личики и с её UA: иначе для Meta это вход
    из чужого места, и она гасит сессию — ровно то, от чего мы уходим. Токен
    наружу в ошибку не кладём никогда.
    """
    info = token_page_debug(raw, proxy, act_id)
    return info["token"], info["error"]


def looks_like_container(value: str) -> bool:
    compact = "".join((value or "").split())
    if not compact or compact.startswith("EAA"):
        return False
    try:
        return bool(decode_session_container(compact).get("token"))
    except ValueError:
        return False



def remote_dns_variant(proxy: str) -> str:
    """Тот же прокси, но с разрешением имени НА СТОРОНЕ ПРОКСИ.

    `socks5://` → `socks5h://`, `socks4://` → `socks4a://`. Разница ровно
    в том, кто превращает `graph.facebook.com` в IP: при `socks5` это делает
    наша машина и только потом отдаёт прокси готовый адрес, при `socks5h` —
    сам прокси, по ту сторону. Если Meta с нашей машины не резолвится, первый
    вариант падает на DNS, не дойдя до прокси, — и выглядит это как «прокси
    не работает», хотя прокси тут ни при чём.

    Сами мы ничего не переписываем: функция только СТРОИТ вариант, чтобы его
    можно было предложить и проверить. Решение — за человеком.
    """
    text = (proxy or "").strip()
    low = text.lower()
    if low.startswith("socks5://"):
        return "socks5h://" + text[len("socks5://"):]
    if low.startswith("socks4://"):
        return "socks4a://" + text[len("socks4://"):]
    return ""


def dns_failed(text: str) -> bool:
    """Похоже ли, что запрос упал именно на разрешении имени.

    Ищем и по сырому тексту исключения, и по нашей же формулировке: сырой
    текст мы обрезаем на 400 символах, и признак «Name or service not known»
    оказывается ЗА обрезом — проверка по одному только сырью тогда молча
    отвечает «нет», хотя это ровно тот случай.
    """
    return bool(re.search(
        r"Name or service not known|nodename nor servname|"
        r"Temporary failure in name|getaddrinfo|"
        r"разрешить имя|имя разрешает наша машина", str(text)))


def probe_proxy(proxy: str, timeout: float = 12.0) -> dict[str, Any]:
    """Дошли ли мы через этот прокси до Meta и каким IP нас оттуда видно.

    Проверка БЕЗ токена: `me` без него отвечает ошибкой «нужен токен», и
    это ровно то, что нам надо знать — запрос дошёл до Meta и она ответила.
    Иначе проверить свежий прокси было бы нечем: токена к нему ещё нет,
    а «привяжу к личике и подожду ночного сбора» — не проверка.

    Внешний IP спрашиваем отдельно и необязательно: он отвечает на вопрос
    «каким гео нас видит Facebook», но если сервис определения IP недоступен,
    это ничего не говорит о самом прокси.
    """
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    started = time.time()
    try:
        resp = session.get(f"{BASE_URL}/{DEFAULT_GRAPH_VERSION}/me",
                           timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — любая сетевая беда тут ожидаема
        return {"ok": False, "ms": int((time.time() - started) * 1000),
                "error": explain_network(exc, has_proxy=bool(proxy))}
    ms = int((time.time() - started) * 1000)
    ip = ""
    try:
        ip = session.get("https://api.ipify.org", timeout=6).text.strip()[:45]
    except Exception:  # noqa: BLE001 — не узнали IP, но прокси при чём
        ip = ""
    return {"ok": True, "ms": ms, "ip": ip, "status": resp.status_code,
            "meta": (resp.json().get("error", {}).get("message", "")
                     if resp.headers.get("content-type", "").startswith(
                         "application/json") else "")[:200]}


_PROXY_SCHEMES = ("socks5h", "socks5", "socks4a", "socks4", "https", "http")


def parse_proxy(raw: str) -> tuple[str, str]:
    """Привести строку прокси к виду, который понимает requests.

    Возвращает `(url, ошибка)`. Данные при этом НЕ меняются: схема остаётся
    ровно той, что дали, хост, порт, логин и пароль — те же самые. Меняется
    только их порядок, и только в одном случае: продавцы прокси и антики
    почти поголовно пишут `host:port:user:pass`, а requests понимает
    исключительно `user:pass@host:port`. Это не «исправление» чужой строки,
    а перестановка тех же четырёх полей в тот синтаксис, который умеет
    разобрать библиотека.

    Пароль и логин экранируем: `@`, `:` и `/` в пароле встречаются постоянно
    и ломают разбор URL — вставленный без экранирования пароль с собакой
    превращал хост в мусор, и прокси «не работал» на ровном месте.
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    scheme, sep, rest = text.partition("://")
    if not sep:
        return "", ("не видно схемы — допишите в начало socks5:// (или http://, "
                    "https://, socks4://): по одной строке нельзя понять, "
                    "какого типа прокси")
    scheme = scheme.strip().lower()
    if scheme not in _PROXY_SCHEMES:
        return "", (f"схема «{scheme}» незнакома — бывают "
                    "socks5, socks5h, socks4, socks4a, http, https")
    rest = rest.strip().rstrip("/")
    if not rest:
        return "", "после схемы пусто — нужен хотя бы хост и порт"

    user = password = ""
    if "@" in rest:
        # Канонический вид. Режем по ПОСЛЕДНЕЙ собаке: в пароле она бывает.
        creds, _, hostport = rest.rpartition("@")
        # …но собака бывает и в пароле, а хвост после неё тогда хостом не
        # выглядит. Если порт справа не число — значит разрезали не там,
        # и правильная граница — первая собака.
        if ":" not in hostport or not hostport.rsplit(":", 1)[-1].isdigit():
            creds, _, hostport = rest.partition("@")
        if creds:
            user, _, password = creds.partition(":")
    else:
        parts = rest.split(":")
        if len(parts) == 2:
            hostport = rest
        elif len(parts) == 4:
            # host:port:user:pass или user:pass:host:port — различаем по тому,
            # где стоит число: порт единственное поле, обязанное быть числом.
            if parts[1].isdigit():
                hostport = f"{parts[0]}:{parts[1]}"
                user, password = parts[2], parts[3]
            elif parts[3].isdigit():
                hostport = f"{parts[2]}:{parts[3]}"
                user, password = parts[0], parts[1]
            else:
                return "", ("не понял, где здесь порт: ни второе, ни последнее "
                            "поле не число")
        else:
            return "", ("ожидал host:port, host:port:логин:пароль или "
                        "логин:пароль@host:port")

    host, _, port = hostport.partition(":")
    host = host.strip()
    port = port.strip()
    if not host:
        return "", "не разобрал хост"
    if not port.isdigit() or not (0 < int(port) < 65536):
        return "", "порт «%s» не похож на порт" % (port or "—")

    if user:
        safe = urllib.parse.quote(user, safe="")
        if password:
            safe += ":" + urllib.parse.quote(password, safe="")
        return f"{scheme}://{safe}@{host}:{port}", ""
    return f"{scheme}://{host}:{port}", ""


def split_secret_and_proxy(text: str) -> tuple[str, str]:
    """«<контейнер> socks5://user:pass@host:port» → (секрет, прокси).

    Прокси необязателен и всегда последний: ни base64-контейнер, ни токен EAA
    пробелов внутри не содержат, поэтому режем по последнему пробелу и берём
    хвост за прокси только если он похож на URL со схемой.
    """
    head, sep, tail = text.strip().rpartition(" ")
    if sep and "://" in tail:
        return head.strip(), tail.strip()
    return text.strip(), ""


def secret_is_complete(secret: str) -> bool:
    """Целый ли секрет.

    Признак — распаковка контейнера, а не длина: Telegram режет сообщение
    ровно по 4096 символам посреди base64, и по длине первый кусок
    неотличим от целого токена.
    """
    secret = (secret or "").strip()
    if not secret:
        return False
    if secret.startswith("EAA"):
        return True
    try:
        decode_session_container(secret)
    except ValueError:
        return False
    return True


def build_session(raw: str, proxy: str = "") -> tuple[requests.Session, str, dict[str, Any]]:
    """(http-сессия, токен, метаданные). Принимает и контейнер, и голый токен."""
    session = requests.Session()
    raw = (raw or "").strip()
    meta: dict[str, Any] = {"kind": "token", "cookies": 0, "ua": ""}

    if raw.startswith("EAA"):
        token = raw
        session.headers["User-Agent"] = "Admetrics/2.0"
    else:
        container = decode_session_container(raw)
        token = str(container["token"]).strip()
        meta["kind"] = "session"
        ua = str(container.get("ua") or "").strip()
        if ua:
            session.headers["User-Agent"] = ua
            meta["ua"] = ua
        for cookie in container.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue
            kwargs: dict[str, Any] = {}
            if cookie.get("domain"):
                kwargs["domain"] = str(cookie["domain"])
            if cookie.get("path"):
                kwargs["path"] = str(cookie["path"])
            session.cookies.set(name, str(cookie.get("value") or ""), **kwargs)
            meta["cookies"] += 1

    if proxy:
        # Прокси используем ровно в том виде, в каком его дали. Схему
        # не правим: строку выдаёт продавец прокси, и он знает, что
        # выдаёт. Если socks5:// не резолвит — об этом скажет
        # explain_network(), решение за человеком, не за нами.
        session.proxies.update({"http": proxy, "https": proxy})
    return session, token, meta


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #
class Graph:
    def __init__(self, session: requests.Session, token: str,
                 version: str = DEFAULT_GRAPH_VERSION, verbose: bool = False,
                 throttle_at: float = 80.0):
        self.session = session
        self.token = token
        self.version = version
        self.verbose = verbose
        self.throttle_at = throttle_at
        self.calls = 0
        self.usage: dict[str, Any] = {}
        self.field_cache: dict[str, tuple[list[str], list[str]]] = {}

    # --- лимиты ---------------------------------------------------------- #
    def _record_usage(self, headers: Any) -> None:
        for header in ("x-app-usage", "x-ad-account-usage",
                       "x-business-use-case-usage"):
            value = headers.get(header)
            if not value:
                continue
            try:
                self.usage[header] = json.loads(value)
            except (TypeError, ValueError):
                self.usage[header] = value

    def worst_usage_pct(self) -> float:
        worst = 0.0

        def scan(node: Any) -> None:
            nonlocal worst
            if isinstance(node, dict):
                for key, val in node.items():
                    if key.endswith("_util_pct") or key in {
                            "call_count", "total_cputime", "total_time",
                            "acc_id_util_pct"}:
                        try:
                            worst = max(worst, float(val))
                        except (TypeError, ValueError):
                            pass
                    else:
                        scan(val)
            elif isinstance(node, list):
                for item in node:
                    scan(item)

        scan(self.usage)
        return worst

    def _respect_limits(self) -> None:
        """Притормозить, если Meta говорит, что бюджет расхода на исходе.
        Дешевле подождать, чем получить 613 и потерять час сбора."""
        pct = self.worst_usage_pct()
        if pct >= 95:
            if self.verbose:
                print("      · лимит ≥95%, пауза 60 с", file=sys.stderr)
            time.sleep(60)
        elif pct > self.throttle_at:
            time.sleep(5)

    # --- запросы --------------------------------------------------------- #
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{BASE_URL}/{self.version}/{str(path).lstrip('/')}"
        query = dict(params or {})
        query["access_token"] = self.token
        last: Exception | None = None

        for attempt in range(MAX_RETRIES):
            self.calls += 1
            try:
                response = self.session.get(url, params=query, timeout=REQUEST_TIMEOUT)
                self._record_usage(response.headers)
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                last = TransientError(
                    f"сеть: {explain_network(exc, bool(self.session.proxies))}")
                if attempt == MAX_RETRIES - 1:
                    raise last from exc
                time.sleep(2 ** attempt)
                continue

            if response.ok and "error" not in payload:
                self._respect_limits()
                return payload

            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            last = classify(error.get("code"), error.get("error_subcode"),
                            error.get("message", str(payload)[:400]),
                            response.status_code)
            transient = isinstance(last, TransientError) or error.get("is_transient")
            if transient and attempt < MAX_RETRIES - 1:
                retry_after = response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2 ** (attempt + 1))
                continue
            raise last

        raise last or TransientError("не удалось выполнить запрос")

    def post(self, node_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Изменить узел Meta: статус, бюджет.

        Единственный метод клиента, который что-то МЕНЯЕТ на стороне Meta,
        и обращаться с ним надо соответственно. Повторов здесь нет намеренно:
        GET можно послать дважды без последствий, POST — нельзя, и «сеть
        подвисла, шлём ещё раз» превращается в две смены бюджета вместо одной.
        Ошибку возвращаем вызывающему; решение повторять принимает человек.

        Вызывающий код обязан спросить подтверждение до вызова: за этой
        строкой чужие деньги.
        """
        url = f"{BASE_URL}/{self.version}/{str(node_id).lstrip('/')}"
        data = {k: v for k, v in fields.items() if v is not None}
        data["access_token"] = self.token
        self.calls += 1
        try:
            response = self.session.post(url, data=data, timeout=REQUEST_TIMEOUT)
            self._record_usage(response.headers)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Связь оборвалась — неизвестно, применилось изменение или нет.
            # Говорим это прямо, а не «не получилось»: разница важная, и по
            # ней человек решает, идти ли смотреть в Ads Manager.
            # Соединение не установилось вовсе — значит до Meta ничего
            # не дошло, и изменение точно не применилось. Обрыв ПОСЛЕ
            # установления — другое дело, там неизвестность настоящая,
            # и говорить о ней надо прямо.
            raw = str(exc)
            never_left = bool(re.search(
                r"Name or service not known|Failed to establish a new connection"
                r"|Connection refused|Max retries exceeded", raw))
            head = ("запрос не ушёл, изменение НЕ применилось"
                    if never_left else
                    "связь оборвалась, применилось ли изменение — неизвестно")
            raise TransientError(
                f"{head}: {explain_network(exc, bool(self.session.proxies))}") from exc
        if response.ok and not (isinstance(payload, dict) and payload.get("error")):
            return payload if isinstance(payload, dict) else {"success": True}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        raise classify(error.get("code"), error.get("error_subcode"),
                       error.get("message", str(payload)[:400]),
                       response.status_code)

    def get_as(self, path: str, token: str,
               params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET под ЧУЖИМ токеном (страницы). Комментарии живут на странице,
        и модерировать их пользовательским токеном нельзя — нужен page-токен."""
        query = dict(params or {})
        query["access_token"] = token
        url = f"{BASE_URL}/{self.version}/{str(path).lstrip('/')}"
        self.calls += 1
        r = self.session.get(url, params=query, timeout=REQUEST_TIMEOUT)
        payload = r.json()
        if not r.ok or (isinstance(payload, dict) and "error" in payload):
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise classify(err.get("code"), err.get("error_subcode"),
                           err.get("message", str(payload)[:200]), r.status_code)
        return payload

    def delete_as(self, node_id: str, token: str) -> bool:
        """DELETE узла под page-токеном. Возвращает True, если удалилось.

        Необратимая операция от имени бренда — вызывающий код обязан
        спрашивать подтверждение или действовать по явному согласию.
        """
        url = f"{BASE_URL}/{self.version}/{node_id}"
        self.calls += 1
        r = self.session.delete(url, params={"access_token": token},
                                timeout=REQUEST_TIMEOUT)
        try:
            payload = r.json()
        except ValueError:
            return r.ok
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            # Уже удалён кем-то — не ошибка для нашей задачи.
            if err.get("code") == 100:
                return False
            raise classify(err.get("code"), err.get("error_subcode"),
                           err.get("message", "delete отклонён"), r.status_code)
        return bool(payload.get("success", r.ok))

    def get_with_fields(self, path: str, fields: list[str],
                        params: dict[str, Any] | None = None,
                        cache_key: str | None = None
                        ) -> tuple[dict[str, Any], list[str], list[str]]:
        """Запросить поля, бинарным поиском отсеяв те, которых Meta не знает.

        Набор доступных полей зависит от версии API, приложения токена и уровня
        доверия аккаунта. Просить заведомо широкий список и отсеивать — надёжнее,
        чем зашивать константу, которая протухнет на следующей версии API.
        """
        if cache_key and cache_key in self.field_cache:
            known_alive, known_dead = self.field_cache[cache_key]
            if not known_alive:
                return {}, [], known_dead
            query = dict(params or {})
            query["fields"] = ",".join(known_alive)
            try:
                return self.get(path, query), list(known_alive), list(known_dead)
            except GraphError:
                pass  # у этого объекта набор полей уже — уходим в bisect

        alive: list[str] = []
        dead: list[str] = []
        merged: dict[str, Any] = {}

        def attempt(chunk: list[str]) -> None:
            if not chunk:
                return
            query = dict(params or {})
            query["fields"] = ",".join(chunk)
            try:
                data = self.get(path, query)
            except GraphError as exc:
                if len(chunk) == 1:
                    dead.append(chunk[0])
                    return
                if not NONEXISTING_FIELD_RE.search(str(exc)) and exc.code not in {100, 12}:
                    raise
                mid = len(chunk) // 2
                attempt(chunk[:mid])
                attempt(chunk[mid:])
                return
            alive.extend(chunk)
            if isinstance(data, dict):
                merged.update(data)

        attempt(list(fields))
        if cache_key:
            self.field_cache[cache_key] = (list(alive), list(dead))
        return merged, alive, dead

    def paginate(self, path: str, params: dict[str, Any] | None = None,
                 max_pages: int = 50) -> Iterator[dict[str, Any]]:
        payload = self.get(path, params)
        pages = 0
        while True:
            yield from payload.get("data", [])
            pages += 1
            next_url = (payload.get("paging") or {}).get("next")
            if not next_url or pages >= max_pages:
                break
            self.calls += 1
            response = self.session.get(next_url, timeout=REQUEST_TIMEOUT)
            self._record_usage(response.headers)
            payload = response.json()
            if not response.ok or "error" in payload:
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                raise classify(error.get("code"), error.get("error_subcode"),
                               error.get("message", "ошибка пагинации"),
                               response.status_code)
            self._respect_limits()

    def paginate_fields(self, path: str, fields: list[str],
                        params: dict[str, Any] | None = None,
                        max_pages: int = 50, cache_key: str | None = None
                        ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        probe_params = dict(params or {})
        probe_params["limit"] = 1
        _, alive, dead = self.get_with_fields(path, fields, probe_params,
                                              cache_key=cache_key)
        if not alive:
            return [], [], dead
        full = dict(params or {})
        full["fields"] = ",".join(alive)
        full.setdefault("limit", 100)
        return list(self.paginate(path, full, max_pages)), alive, dead

    # --- batch ----------------------------------------------------------- #
    def batch(self, relative_urls: list[str]) -> list[dict[str, Any]]:
        """До 50 подзапросов одним HTTP-вызовом.

        Meta отдаёт массив в порядке запросов; на каждый элемент — свой
        `code` и `body` строкой. Ошибка одного подзапроса не роняет остальные,
        поэтому результат нормализуется в {ok, data|error} по каждой позиции.
        """
        out: list[dict[str, Any]] = []
        for start in range(0, len(relative_urls), BATCH_LIMIT):
            chunk = relative_urls[start:start + BATCH_LIMIT]
            spec = [{"method": "GET", "relative_url": u} for u in chunk]
            self.calls += 1
            response = self.session.post(
                f"{BASE_URL}/{self.version}/",
                data={"access_token": self.token, "batch": json.dumps(spec)},
                timeout=REQUEST_TIMEOUT)
            self._record_usage(response.headers)
            try:
                payload = response.json()
            except ValueError as exc:
                raise TransientError(f"batch: некорректный ответ: {hide_secrets(exc)}") from exc
            if isinstance(payload, dict) and "error" in payload:
                error = payload["error"]
                raise classify(error.get("code"), error.get("error_subcode"),
                               error.get("message", "batch отклонён"),
                               response.status_code)
            for item in payload or []:
                if not item:
                    out.append({"ok": False, "error": "пустой ответ подзапроса"})
                    continue
                try:
                    body = json.loads(item.get("body") or "{}")
                except ValueError:
                    out.append({"ok": False, "error": "тело подзапроса не JSON"})
                    continue
                if int(item.get("code", 500)) >= 400 or "error" in body:
                    err = body.get("error", {})
                    out.append({"ok": False, "error": err.get("message", "ошибка"),
                                "code": err.get("code"),
                                "subcode": err.get("error_subcode")})
                else:
                    out.append({"ok": True, "data": body})
            self._respect_limits()
        return out


# --------------------------------------------------------------------------- #
# Расшифровки кодов Meta
# --------------------------------------------------------------------------- #
ACCOUNT_STATUS = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT", 9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE",
    101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}
ACCOUNT_STATUS_RU = {
    1: "активен", 2: "забанен", 3: "не оплачен", 7: "на проверке рисков",
    8: "ждёт оплаты", 9: "льготный период", 100: "закрывается", 101: "закрыт",
}
DISABLE_REASON = {
    0: "NONE", 1: "ADS_INTEGRITY_POLICY", 2: "ADS_IP_REVIEW",
    3: "RISK_PAYMENT", 4: "GRAY_ACCOUNT_SHUT_DOWN", 5: "ADS_AFC_REVIEW",
    6: "BUSINESS_INTEGRITY_RAR", 7: "PERMANENT_CLOSE",
    8: "UNUSED_RESELLER_ACCOUNT", 9: "UNUSED_ACCOUNT",
    10: "UMBRELLA_AD_ACCOUNT", 11: "BUSINESS_MANAGER_INTEGRITY_POLICY",
    12: "MISREPRESENTATION_POLICY", 13: "AFC_FINANCIAL_POLICY",
    14: "SCRAPING_RISK", 15: "COMPROMISED_RISK",
}
DISABLE_REASON_RU = {
    1: "нарушение рекламной политики", 2: "проверка прав на ИС",
    3: "рискованная оплата", 4: "серый аккаунт", 5: "финансовая проверка",
    6: "проверка бизнес-целостности", 7: "закрыт навсегда",
    11: "нарушение политики BM", 12: "введение в заблуждение",
    14: "риск скрейпинга", 15: "аккаунт скомпрометирован",
}
