"""Keitaro: кампании трекера и сопоставление их со ссылками объявлений.

Задача одна: по ссылке, которую льёт маркетолог, узнать id кампании в
Keitaro. Всё остальное здесь — обвязка вокруг неё.

Почему сопоставление вообще возможно. У каждой кампании в Keitaro свой
домен (`domain`) и свой алиас (`alias`), а ссылка кампании — это
`домен/алиас`. Ровно она и стоит в объявлении. Значит достаточно разобрать
ссылку объявления на хост и первый сегмент пути и найти кампанию с такой
парой.

Две оговорки, из-за которых код сложнее одной строки:

* на одном домене может висеть несколько кампаний — тогда хоста мало,
  и алиас обязателен;
* в объявлении часто стоит голый домен без пути (Keitaro умеет отдавать
  кампанию по умолчанию) — тогда алиаса нет, и однозначный ответ есть
  только если на домене ровно одна кампания.

Оба случая различаются явно и помечаются в результате, чтобы «нашли точно»
и «нашли, потому что больше некому» не смешивались в одну кучу.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Iterator

import requests

# Замерено на боевом трекере: до десяти кампаний за запрос — четверть
# секунды, на двадцати пяти он отдаёт заголовки и залипает. Не геройствуем.
PAGE = 10
TIMEOUT = 45


class KeitaroError(RuntimeError):
    """Трекер не ответил или отказал. Сбор от этого падать не должен."""


def configured() -> bool:
    return bool(os.environ.get("KEITARO_URL", "").strip()
                and os.environ.get("KEITARO_KEY", "").strip())


def _get(path: str, **params: Any) -> Any:
    """Чтение Admin API.

    Только GET. POST на тех же путях в Keitaro означает СОЗДАНИЕ объекта, и
    ключу «только чтение» он отвечает 403 «нет прав на Campaigns» — текст,
    который легко прочесть как «доступ не выдан». Один раз уже прочли.
    """
    base = os.environ.get("KEITARO_URL", "").strip().rstrip("/")
    key = os.environ.get("KEITARO_KEY", "").strip()
    if not base or not key:
        raise KeitaroError("не заданы KEITARO_URL / KEITARO_KEY")
    url = f"{base}/admin_api/v1/{path.lstrip('/')}"
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT,
                            headers={"Api-Key": key})
    except requests.exceptions.Timeout as exc:
        raise KeitaroError(f"трекер не ответил за {TIMEOUT} с") from exc
    except Exception as exc:  # noqa: BLE001
        raise KeitaroError(f"не достучались до трекера: {exc}") from exc
    if resp.status_code == 401:
        raise KeitaroError("ключ не принят (401) — проверьте KEITARO_KEY")
    if resp.status_code == 403:
        raise KeitaroError("нет прав на этот раздел (403) — в Keitaro "
                           "у пользователя ключа должен быть доступ к "
                           "кампаниям хотя бы на чтение")
    if resp.status_code != 200:
        raise KeitaroError(f"трекер ответил {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise KeitaroError("ответ трекера не разобрался как JSON") from exc


def fetch_campaigns() -> list[dict[str, Any]]:
    """Все кампании трекера, постранично.

    Размер страницы уменьшаем при таймауте, а не сдаёмся: сколько потянет
    конкретный трекер, заранее не знает никто, и зависит это от его
    нагрузки в момент запроса.
    """
    out: list[dict[str, Any]] = []
    offset, page = 0, PAGE
    while True:
        try:
            chunk = _get("campaigns", limit=page, offset=offset)
        except KeitaroError:
            if page > 2:
                page = max(2, page // 2)
                continue
            raise
        if not isinstance(chunk, list):
            raise KeitaroError("ожидался список кампаний")
        out.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
        if offset > 50000:            # предохранитель от битой пагинации
            break
    return out


def campaign_row(c: dict[str, Any]) -> dict[str, Any]:
    """Кампания трекера в том виде, в каком мы её храним."""
    domain = str(c.get("domain") or "").strip()
    return {
        "kt_id": int(c.get("id") or 0),
        "alias": str(c.get("alias") or "").strip(),
        "name": str(c.get("name") or ""),
        "grp": str(c.get("group") or ""),
        "domain": domain,
        "host": host_of(domain),
        "state": str(c.get("state") or ""),
        "token": str(c.get("token") or ""),
        "traffic_source_id": int(c.get("traffic_source_id") or 0),
    }


def host_of(url: str) -> str:
    """Хост из адреса, приведённый к сравнимому виду.

    `www.` отбрасываем, регистр опускаем: в объявлении и в трекере один и
    тот же домен пишут по-разному, и без нормализации совпадение теряется
    на ровном месте.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = (urllib.parse.urlparse(text).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def alias_of(url: str) -> str:
    """Первый сегмент пути — он же алиас кампании.

    Всё, что дальше первого сегмента, к алиасу не относится: там обычно
    лежат подпапки лендинга. Параметры запроса тем более не при чём.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        path = urllib.parse.urlparse(text).path or ""
    except ValueError:
        return ""
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else ""


# Домен внутри произвольного текста. Ищем именно так, потому что в названии
# кампании он лежит среди слов: «PL FB gustier-telephonists.click», «IT FB
# receivers-tallied.click fbm». Требуем хотя бы одну точку и зону из букв —
# иначе в «домены» попадут куски вроде «16.09».
_HOST_IN_TEXT = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)


def hosts_of_campaign(c: dict[str, Any]) -> list[tuple[str, str]]:
    """Все домены, по которым эту кампанию можно узнать, и откуда они взяты.

    Поле `domain` заполнено у единиц: на боевом трекере оно пустое у 332
    кампаний из 337. Зато домен почти всегда записан в НАЗВАНИИ кампании —
    так его туда кладёт команда. Формально это не поле API, а соглашение
    людей, но именно оно и связывает объявление с кампанией, а значит
    игнорировать его — значит не решить задачу вовсе.

    Источник возвращаем вместе с доменом: «взято из поля» и «вычитано из
    названия» — разной надёжности основания, и в отчёте они должны
    различаться.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    direct = host_of(c.get("domain") or "")
    if direct:
        out.append((direct, "домен кампании"))
        seen.add(direct)

    for m in _HOST_IN_TEXT.finditer(str(c.get("name") or "")):
        host = m.group(1).lower()
        if host.startswith("www."):
            host = host[4:]
        # Отсекаем то, что домен только напоминает: дата «16.09», версия
        # «2.0». Зона из двух и более букв уже отсеяна регуляркой, здесь
        # добиваем случай, когда всё имя — цифры с точкой.
        if host in seen or host.replace(".", "").isdigit():
            continue
        out.append((host, "домен из названия"))
        seen.add(host)
    return out


class Matcher:
    """Индекс кампаний трекера и поиск по ссылке объявления.

    Строится один раз на прогон: 337 кампаний — это 337 словарных записей,
    а ссылок в сборе тысячи, и ходить в базу на каждую было бы расточительно.
    """

    def __init__(self, rows: list[dict[str, Any]]):
        self.by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        self.by_host: dict[str, list[dict[str, Any]]] = {}
        self.source: dict[str, str] = {}
        for r in rows:
            alias = (r.get("alias") or "").lower()
            for host, origin in hosts_of_campaign(r):
                if alias:
                    self.by_pair.setdefault((host, alias), r)
                self.by_host.setdefault(host, []).append(r)
                self.source.setdefault(host, origin)

    def match(self, link: str) -> tuple[dict[str, Any] | None, str]:
        """Найти кампанию по ссылке. Возвращает (кампания, как нашли).

        Способ возвращаем наружу намеренно: «совпал домен и алиас» и
        «на домене всё равно одна кампания» — разной надёжности ответы, и
        смешивать их в одно «нашли» значит терять право усомниться потом.
        """
        host = host_of(link)
        if not host:
            return None, "нет ссылки"
        alias = alias_of(link).lower()
        exact = self.by_pair.get((host, alias)) if alias else None
        if exact:
            return exact, "домен и алиас"
        same = self.by_host.get(host) or []
        if not same:
            return None, "домен не найден в трекере"
        origin = self.source.get(host, "домен")
        if len(same) == 1:
            return same[0], origin
        # Несколько кампаний на домене и алиас не подошёл: гадать нельзя.
        # Активная одна — берём её; иначе честно говорим, что неоднозначно.
        active = [r for r in same if r.get("state") == "active"]
        if len(active) == 1:
            return active[0], f"{origin}, одна активная из нескольких"
        return None, f"на домене {len(same)} кампаний, точного совпадения нет"
