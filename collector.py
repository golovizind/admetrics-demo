#!/usr/bin/env python3
"""
Коллектор Admetrics.

Обходит личики маркетологов и наполняет три слоя: доступы, активы, факты.
Отличия от старого коллектора — не косметические, каждое закрывает конкретный
дефект прототипа:

  • **личек у маркетолога много.** Единица обхода — `identity`, а не человек;
  • **кабинет попадает в реестр фактом существования**, а не фактом трат:
    забаненный до открута кабинет — тоже актив, и бизнес просит его считать;
  • **суточный снапшот** каждого кабинета — из его дифа берутся все алерты;
  • **скользящее окно** вместо `date_preset=maximum` каждые 4 часа;
  • **AuthError отделён от TransientError**: мёртвая личка помечается
    `needs_reconnect` с человеческой причиной, временная ошибка не портит статус;
  • **прогресс переживает падение** — единицы работы лежат в `collection_tasks`.

Запуск:
    python3 collector.py                    # скользящее окно 7 дней
    python3 collector.py --days 28          # недельный глубокий проход
    python3 collector.py --since 2026-08-01 --until 2026-08-12
    python3 collector.py --registry-only    # только реестр и снапшоты, без статистики
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core import alerts, cards, graph, keitaro, platforms, sheet, store
from core.graph import (AuthError, GraphError, TransientError,
                        dns_failed, remote_dns_variant, probe_proxy)


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Прогон
# --------------------------------------------------------------------------- #
def start_run(kind: str) -> int:
    with store.connect() as conn:
        cur = conn.execute(store._q("""
            INSERT INTO collection_runs (started_at, kind, status)
            VALUES (?, ?, 'running')
        """), (store.now(), kind))
        if store.PG:  # pragma: no cover
            cur = conn.execute("SELECT lastval() AS id")
            return int(store.rows(cur)[0]["id"])
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, stats: dict[str, Any]) -> None:
    with store.connect() as conn:
        conn.execute(store._q("""
            UPDATE collection_runs
               SET finished_at = ?, status = ?, stats_json = ?
             WHERE id = ?
        """), (store.now(), status,
               json.dumps(stats, ensure_ascii=False, default=str), run_id))


def record_task(run_id: int, identity_id: str | None, act_id: str | None,
                kind: str, status: str, error: str = "",
                since: str = "", until: str = "") -> None:
    """Единица работы в журнале. Нужна не для красоты: без неё падение на
    середине обхода означает повтор с нуля, а на 600 кабинетах это дорого."""
    with store.connect() as conn:
        conn.execute(store._q("""
            INSERT INTO collection_tasks
                (run_id, identity_id, act_id, kind, since, until,
                 status, attempts, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """), (run_id, identity_id, act_id, kind, since or None, until or None,
               status, error[:500], store.now()))


# --------------------------------------------------------------------------- #
# Обход одной личики
# --------------------------------------------------------------------------- #
def _try_remote_dns(entry: dict[str, Any], exc: Exception) -> str:
    """Перевести личку на разрешение имени стороной прокси — если это чинит.

    Возвращает новый адрес (для лога) или пустую строку, если не применимо
    либо не помогло. Условия жёсткие намеренно: только падение на DNS, только
    socks-схема без `h`, и только если проверка через `h` реально прошла.
    Всё остальное — не наш случай, и адрес остаётся тем, что дали.
    """
    proxy = entry.get("proxy") or ""
    if not proxy or not dns_failed(str(exc)):
        return ""
    alt = remote_dns_variant(proxy)
    if not alt:
        return ""
    try:
        res = probe_proxy(alt, timeout=12.0)
    except Exception:  # noqa: BLE001 — проверка не должна ронять сбор
        return ""
    if not res.get("ok"):
        return ""
    store.set_identity_proxy(entry["id"], alt)
    # Статус после смены сбрасывается в unknown — это верно для ручной
    # правки, но здесь мы тут же переспрашиваем Meta, так что «не проверена»
    # продержится секунды. Запись в журнал останется навсегда: по ней потом
    # видно, что адрес менял не человек.
    store.note_identity_issue(
        entry["id"], f"прокси переведён на разрешение имени стороной прокси: "
                     f"{res.get('ip') or 'IP не определён'}")
    return alt


def collect_identity(entry: dict[str, Any], run_id: int, since: str, until: str,
                     registry_only: bool = False,
                     tx_days: int = 45) -> dict[str, int]:
    """Собрать всё, что видно одной личкой.

    Порядок намеренный: сначала health-check, потом реестр, и только потом
    статистика. Если личка мертва, нет смысла ходить за инсайтами — а старый
    коллектор именно это и делал, накапливая ошибки по каждому кабинету.
    """
    stats = {"accounts": 0, "spend_rows": 0, "geo_rows": 0, "tx_rows": 0,
             "campaign_rows": 0, "ad_rows": 0, "breakdown_rows": 0,
             "campaigns": 0, "adsets": 0, "ads": 0, "rejects": 0, "alerts": 0, "errors": 0}
    identity_id = entry["id"]
    who = entry.get("label") or entry.get("marketer_name") or identity_id

    # Обновляем токен из живой сессии ПЕРЕД сбором. Токен из расширения/ФБТула
    # живёт часы, а куки — месяцами; поэтому не полагаемся на снимок в базе, а
    # добываем свежий токен из тех же куков, как это делает сам Ads Manager.
    # Снимок перестаёт протухать: пока жива сессия, жив и сбор.
    secret = entry.get("secret", "")
    proxy = entry.get("proxy", "")
    if graph.looks_like_container(secret):
        try:
            fresh, used_proxy, rerr = graph.refresh_container(secret, proxy)
        except Exception as exc:  # noqa: BLE001 — обновление не должно ронять сбор
            fresh, used_proxy, rerr = "", proxy, f"{type(exc).__name__}"
        if fresh:
            store.update_identity_token(identity_id, fresh,
                                        used_proxy if used_proxy != proxy else "")
            secret, proxy = fresh, used_proxy
            log(f"  [ТОКЕН] {who}: обновлён из сессии")
        elif rerr:
            # Не удалось — не приговор: старый токен ещё может сработать, и его
            # проверит whoami ниже. Но если сессия истекла, честно скажем.
            log(f"  [ТОКЕН] {who}: обновить не вышло ({rerr}) — пробую как есть")

    try:
        adapter = platforms.adapter_for(entry.get("platform", "fb"),
                                        secret, proxy)
        me = adapter.whoami()
    except AuthError as exc:
        store.set_identity_status(identity_id, "needs_reconnect", exc.human,
                                  exc.code, exc.subcode, raw=str(exc)[:400])
        alerts.identity_dead(identity_id, entry.get("marketer_id", ""),
                             who, exc.human)
        record_task(run_id, identity_id, None, "whoami", "auth_error", str(exc))
        log(f"  [МЁРТВАЯ] {who}: {exc.human}")
        stats["errors"] += 1
        return stats
    except (TransientError, ValueError) as exc:
        # Упали на разрешении имени, а прокси — socks5/socks4? Тогда, прежде
        # чем записывать ошибку, пробуем ТОТ ЖЕ прокси с разрешением имени на
        # его стороне. Молча схему мы не правим и раньше не правили: правим
        # только после того, как исходный вариант доказанно не прошёл, а
        # второй — доказанно прошёл. Проверка, а не догадка.
        fixed = _try_remote_dns(entry, exc)
        if fixed:
            log(f"  [ПРОКСИ] {who}: socks5:// не резолвит имя — перевёл на "
                f"{fixed}, повторяю")
            record_task(run_id, identity_id, None, "proxy_dns", "fixed", fixed)
            entry = {**entry, "proxy": store.identity_with_secret(identity_id)
                     .get("proxy", "")}
            return collect_identity(entry, run_id, since, until,
                                    registry_only, tx_days)
        # Временная ошибка НЕ меняет статус личики: пометить живую личку мёртвой
        # из-за таймаута — значит зря дёрнуть маркетолога и потерять доверие.
        # Но причину записываем: иначе новая личка навсегда остаётся «ещё
        # не проверена» без единого слова о том, что мешает её проверить.
        store.note_identity_issue(identity_id, str(exc))
        record_task(run_id, identity_id, None, "whoami", "transient", str(exc))
        log(f"  [ВРЕМЕННО] {who}: {exc}")
        stats["errors"] += 1
        return stats

    store.set_identity_status(identity_id, "ok", "")
    _remember_platform_user(identity_id, me.get("id", ""), me.get("name", ""))
    log(f"  [OK] {who}: {me.get('name', me.get('id', '?'))}")

    # Права токена. Спрашиваются первыми: если чего-то не хватает, дальнейшие
    # пустые ответы Meta объясняются именно этим, а не поломкой сбора.
    try:
        perms = adapter.fetch_permissions()
        if perms:
            store.replace_permissions(identity_id, perms)
            missing = store.missing_permissions(identity_id)
            if missing:
                log("     нет прав: " + ", ".join(
                    f"{k} ({why})" for k, why in missing))
        stats["perms"] = len(perms)
    except GraphError as exc:
        record_task(run_id, identity_id, None, "permissions", "error", str(exc))

    # --- реестр BM и кабинетов ------------------------------------------ #
    try:
        for biz in adapter.fetch_businesses():
            _upsert_business(biz)
            # Кредитная линия — деньги, которых нет ни в балансе кабинета,
            # ни в списаниях, но которые определяют потолок открута.
            store.set_business_credit(biz["bm_id"],
                                      adapter.fetch_business_credit(biz["bm_id"]))
            _collect_business_control(run_id, identity_id, adapter,
                                      biz["bm_id"], stats)
        accounts = adapter.discover_accounts()
    except AuthError as exc:
        store.set_identity_status(identity_id, "needs_reconnect", exc.human,
                                  exc.code, exc.subcode, raw=str(exc)[:400])
        record_task(run_id, identity_id, None, "discover", "auth_error", str(exc))
        stats["errors"] += 1
        return stats
    except GraphError as exc:
        record_task(run_id, identity_id, None, "discover", "error", str(exc))
        log(f"     реестр не собран: {exc}")
        stats["errors"] += 1
        return stats

    today = date.today().isoformat()
    for account in accounts:
        # Сравнение со СТАРОЙ строкой обязано идти ДО upsert: реестр и есть
        # «последнее известное состояние», после записи разницы уже не будет.
        before = store.get_ad_account(account["act_id"])
        stats["alerts"] += alerts.emit_account_changes(before, account)
        store.upsert_ad_account(account)
        _remember_access(account["act_id"], identity_id)
        store.save_snapshot(account["act_id"], today, account)
        stats["accounts"] += 1
    record_task(run_id, identity_id, None, "discover", "ok")
    log(f"     кабинетов: {len(accounts)}")

    # Page-токены страниц этой личики — для модерации комментариев. Без них
    # удалять комментарии нельзя: они живут на странице, а не на кабинете.
    try:
        for page in adapter.fetch_pages():
            store.save_page_token(page["page_id"], page["name"], page["token"],
                                  identity_id, entry.get("marketer_id"),
                                  page["can_moderate"])
            stats["pages"] = stats.get("pages", 0) + 1
    except GraphError as exc:
        record_task(run_id, identity_id, None, "pages", "error", str(exc))

    if registry_only:
        return stats

    # --- факты ------------------------------------------------------------ #
    for account in accounts:
        act_id = account["act_id"]
        currency = account.get("currency", "")
        # Закрытые кабинеты не опрашиваем: данных там нет, а квота тратится.
        if account.get("account_status") in (101, 100):
            continue
        acc_since, acc_until = _spend_window(account, since, until)
        if acc_since != since:
            log(f"     [{act_id}] первый сбор — беру историю с {acc_since}")
        try:
            daily = adapter.fetch_spend(act_id, acc_since, acc_until, currency)
            stats["spend_rows"] += store.upsert_spend_daily(daily)

            geo = adapter.fetch_spend_countries(act_id, acc_since, acc_until,
                                                currency)
            stats["geo_rows"] += store.upsert_spend_country(geo)

            camp = adapter.fetch_spend_campaigns(act_id, acc_since, acc_until,
                                                 currency)
            stats["campaign_rows"] += _upsert_campaign_spend(camp)

            ads_spend = adapter.fetch_spend_ads(act_id, acc_since, acc_until,
                                                currency)
            stats["ad_rows"] += store.upsert_spend_ads(ads_spend)

            # Разбивки — по одной на измерение, каждая отдельным запросом.
            # Падение одной не должно лишать остальных: набор полей у разных
            # приложений разный, и что-то из этого может быть закрыто.
            for kind in platforms.BREAKDOWNS:
                try:
                    rows_b = adapter.fetch_breakdown(act_id, kind, acc_since,
                                                     acc_until, currency)
                    stats["breakdown_rows"] += store.upsert_breakdowns(rows_b)
                except GraphError as exc:
                    record_task(run_id, identity_id, act_id, f"bd:{kind}",
                                "error", str(exc))

            record_task(run_id, identity_id, act_id, "spend", "ok",
                        since=acc_since, until=acc_until)
        except AuthError as exc:
            store.set_identity_status(identity_id, "needs_reconnect", exc.human,
                                      exc.code, exc.subcode)
            record_task(run_id, identity_id, act_id, "spend", "auth_error", str(exc))
            stats["errors"] += 1
            return stats
        except GraphError as exc:
            record_task(run_id, identity_id, act_id, "spend", "error", str(exc))
            log(f"     [{act_id}] статистика: {exc}")
            stats["errors"] += 1

        try:
            for c in adapter.fetch_campaign_entities(act_id):
                store.upsert_campaign({**c, "issues_json": json.dumps(
                    c.pop("issues", []), ensure_ascii=False)})
                stats["campaigns"] += 1
            for aset in adapter.fetch_adset_entities(act_id):
                store.upsert_adset(aset)
                stats["adsets"] = stats.get("adsets", 0) + 1
            for ad in adapter.fetch_ad_entities(act_id):
                issues = ad.pop("issues", [])
                creative = ad.pop("creative", None)
                store.upsert_ad({**ad, "issues_json": json.dumps(
                    issues, ensure_ascii=False)})
                if creative:
                    store.upsert_ad_creative(ad["ad_id"], creative)
                stats["ads"] += 1
                if issues:
                    stats["rejects"] += _alert_reject(act_id, ad, issues)
            record_task(run_id, identity_id, act_id, "entities", "ok")
        except GraphError as exc:
            record_task(run_id, identity_id, act_id, "entities", "error", str(exc))

        # Контроль доступа и история изменений. Отдельным блоком, а не внутри
        # «сущностей»: эти три ручки доступны не каждому токену, и падение
        # любой из них не должно уносить с собой кампании и объявления.
        try:
            users = adapter.fetch_account_users(act_id)
            if users:
                store.replace_account_users(act_id, users)
                stats["acct_users"] = stats.get("acct_users", 0) + len(users)
            for ev in adapter.fetch_activities(act_id):
                store.upsert_activity(ev)
                stats["activities"] = stats.get("activities", 0) + 1
            for px in adapter.fetch_pixels(act_id):
                store.upsert_pixel(px)
                stats["pixels"] = stats.get("pixels", 0) + 1
            # Автоправила — рядом с журналом намеренно: журнал показывает
            # изменение, правило объясняет его причину, и порознь они
            # отвечают на половину вопроса каждый.
            for rule in adapter.fetch_ad_rules(act_id):
                store.upsert_ad_rule(rule)
                stats["rules"] = stats.get("rules", 0) + 1
            record_task(run_id, identity_id, act_id, "control", "ok")
        except GraphError as exc:
            record_task(run_id, identity_id, act_id, "control", "error", str(exc))

        try:
            for tx in adapter.fetch_transactions(act_id, tx_days):
                store.upsert_fb_transaction(tx)
                stats["tx_rows"] += 1
            record_task(run_id, identity_id, act_id, "transactions", "ok")
        except GraphError as exc:
            # Транзакции доступны не каждому токену. Это не повод валить прогон:
            # без них работает всё, кроме сверки с картами.
            record_task(run_id, identity_id, act_id, "transactions", "error", str(exc))

    return stats


def _collect_business_control(run_id: int, identity_id: str, adapter: Any,
                              bm_id: str, stats: dict[str, int]) -> None:
    """Люди, страницы и пиксели бизнеса.

    Отдельной функцией и в своём try, а не внутри цикла реестра: эти рёбра
    открыты не каждому токену, и отказ по одному BM не должен уносить
    с собой список кабинетов — ради которого прогон вообще затевался.
    """
    try:
        people = adapter.fetch_business_users(bm_id)
        if people:
            store.replace_business_users(bm_id, people)
            stats["bm_users"] = stats.get("bm_users", 0) + len(people)
        pages = adapter.fetch_business_pages(bm_id)
        if pages:
            store.replace_business_pages(bm_id, pages)
            stats["bm_pages"] = stats.get("bm_pages", 0) + len(pages)
        for px in adapter.fetch_business_pixels(bm_id):
            store.upsert_pixel(px)
            stats["pixels"] = stats.get("pixels", 0) + 1
        record_task(run_id, identity_id, None, "bm_control", "ok")
    except GraphError as exc:
        record_task(run_id, identity_id, None, "bm_control", "error", str(exc))


def collect_cards(run_id: int, days: int = 45) -> dict[str, int]:
    """Забрать платежи MyBroCards — вторую половину сверки.

    Окно шире, чем у Meta: финальный статус платежа приходит часами позже,
    а pending может уйти в declined. Перезабор по ключу `payment_id`
    переписывает строку на месте, поэтому переходы состояний не плодят
    дублей и не задваивают сумму.
    """
    stats = {"card_payments": 0, "cards_seen": 0, "errors": 0}
    if not os.environ.get("MYBROCARD_API_KEY", "").strip():
        return stats
    try:
        api = cards.MyBroCards()
        since, until = cards.default_window(days)
        payments = api.fetch_payments(since, until)
    except cards.CardsError as exc:
        record_task(run_id, None, None, "cards", "error", str(exc))
        log(f"  [КАРТЫ] {exc}")
        stats["errors"] += 1
        return stats

    # Кто владеет картой — из платежа же: он несёт email и имя пользователя.
    # Это надёжнее сопоставления по последним четырём цифрам, которых на
    # тринадцать тысяч карт заведомо не хватает на всех.
    by_email = {}
    for m in store.list_marketers():
        email = str(m.get("login") or "").strip().lower()
        if email:
            by_email[email] = m["id"]

    seen_cards = set()
    for row in payments:
        row["marketer_id"] = by_email.get(row["user_email"].lower())
        store.upsert_card_payment(row)
        stats["card_payments"] += 1
        seen_cards.add(row["card_id"])
    stats["cards_seen"] = len(seen_cards)

    # Дотяжка истории. Обычное окно узкое (45 дней), а «за всё время» должно
    # быть за всё время. Тянуть всю историю каждый прогон — жечь лимиты, а
    # разовый скрипт — ручная работа, о которой все забывают. Поэтому каждый
    # прогон подтягивает один кусок старее уже собранного и двигает метку
    # назад, пока не упрётся в пол-дату. Дальше метка стоит и лишних запросов
    # нет. Догоняется само за несколько прогонов.
    stats["cards_backfilled"] = _backfill_cards_chunk(api, by_email, since)

    record_task(run_id, None, None, "cards", "ok", since=since, until=until)
    log(f"  [КАРТЫ] платежей {stats['card_payments']} за {since}..{until}, "
        f"карт {stats['cards_seen']}, запросов {api.calls}"
        + (f", истории +{stats['cards_backfilled']}"
           if stats["cards_backfilled"] else ""))
    return stats


# Докуда вглубь тянем историю платежей и каким шагом за один прогон.
CARDS_HISTORY_FLOOR = os.environ.get("CARDS_HISTORY_FLOOR", "2023-01-01")
CARDS_BACKFILL_CHUNK_DAYS = int(os.environ.get("CARDS_BACKFILL_CHUNK_DAYS", "90"))


def _backfill_cards_chunk(api: "cards.MyBroCards", by_email: dict,
                          recent_since: str) -> int:
    """Один шаг дотяжки истории платежей вглубь. Возвращает сколько записано.

    Метка `cards_backfill_cursor` — самая ранняя дата, докуда уже дотянули.
    Пусто — начинаем от края обычного окна. Дойдя до пол-даты, ставим
    `cards_backfill_done` и больше не ходим.
    """
    if store.kv_get("cards_backfill_done") == "1":
        return 0
    cursor = store.kv_get("cards_backfill_cursor") or recent_since
    end = date.fromisoformat(cursor)
    floor = date.fromisoformat(CARDS_HISTORY_FLOOR)
    if end <= floor:
        store.kv_set("cards_backfill_done", "1")
        return 0
    start = max(floor, end - timedelta(days=CARDS_BACKFILL_CHUNK_DAYS))
    try:
        payments = api.fetch_payments(start.isoformat(), end.isoformat())
    except cards.CardsError as exc:
        log(f"  [КАРТЫ] дотяжку истории отложил: {exc}")
        return 0
    n = 0
    for row in payments:
        row["marketer_id"] = by_email.get(row["user_email"].lower())
        store.upsert_card_payment(row)
        n += 1
    store.kv_set("cards_backfill_cursor", start.isoformat())
    if start <= floor:
        store.kv_set("cards_backfill_done", "1")
    return n


def clean_page_comments(page: dict, delete: bool = False,
                        max_posts: int = 50) -> dict[str, int]:
    """Собрать комментарии страницы и, если delete=True, удалить их все.

    Источники два: посты самой страницы (ФП) и рекламные посты — их
    `story_id` лежит в объявлениях кабинетов этой страницы. delete=False —
    только подсчёт (безопасный предпросмотр перед необратимым удалением).

    Работает под page-токеном, поэтому мультитокен-владелец нужен лишь
    ради его прокси — ходить тем же IP, что и при фарме.
    """
    result = {"comments": 0, "deleted": 0, "posts": 0, "errors": 0}
    token = page.get("token")
    if not token:
        return result
    # Адаптер ради сессии/прокси владеющей личики; операции идут page-токеном.
    entry = None
    for e in store.active_identities():
        if e["id"] == page.get("identity_id"):
            entry = e
            break
    secret = entry["secret"] if entry else ""
    proxy = entry.get("proxy", "") if entry else ""
    try:
        adapter = platforms.adapter_for("fb", secret or "EAA_none", proxy)
    except Exception:  # noqa: BLE001
        return result

    # Источники комментариев: посты ФП + рекламные story_id.
    post_ids = set(adapter.page_post_ids(page["page_id"], token, max_posts))
    with store.connect() as conn:
        stories = store.rows(conn.execute(store._q("""
            SELECT DISTINCT ad.story_id
              FROM ads ad
              JOIN ad_account_access x ON x.act_id = ad.act_id
             WHERE x.identity_id = ? AND ad.story_id != ''
        """), (page.get("identity_id"),)))
    post_ids.update(s["story_id"] for s in stories if s["story_id"])

    for pid in post_ids:
        result["posts"] += 1
        for c in adapter.list_comments(pid, token):
            result["comments"] += 1
            if delete:
                try:
                    if adapter.delete_comment(c["comment_id"], token):
                        result["deleted"] += 1
                except GraphError:
                    result["errors"] += 1
    if delete:
        store.mark_page_cleaned(page["page_id"], result["deleted"])
    return result


def _upsert_campaign_spend(rows_in: list[dict[str, Any]]) -> int:
    """Суточный факт по кампаниям. Отдельно от сущностей: тут история открута,
    там текущее состояние."""
    stamp = store.now()
    written = 0
    with store.connect() as conn:
        for r in rows_in:
            if not r.get("campaign_id"):
                continue
            payload = (r["date"], "fb", r["campaign_id"], r["act_id"],
                       r.get("campaign_name", ""), r.get("objective", ""),
                       r.get("currency", ""), r.get("spend", 0),
                       r.get("impressions", 0), r.get("clicks", 0),
                       r.get("leads", 0), r.get("purchases", 0),
                       r.get("revenue", 0))
            conn.execute(store._q("""
                INSERT INTO spend_campaign_daily
                    (date, platform, campaign_id, act_id, campaign_name,
                     objective, currency, spend, impressions, clicks,
                     leads, purchases, revenue, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (date, platform, campaign_id) DO UPDATE SET
                    act_id = ?, campaign_name = ?, objective = ?, currency = ?,
                    spend = ?, impressions = ?, clicks = ?, leads = ?,
                    purchases = ?, revenue = ?, updated_at = ?
            """), (*payload, stamp, *payload[3:], stamp))
            written += 1
    return written


def _alert_reject(act_id: str, ad: dict[str, Any], issues: list[Any]) -> int:
    """Реджект объявления — с текстом причины, чтобы маркетолог не шёл
    искать его руками в Ads Manager."""
    summary = ""
    for issue in issues:
        if isinstance(issue, dict):
            summary = (issue.get("error_summary")
                       or issue.get("error_message") or "")
            if summary:
                break
    return int(alerts.raise_alert(
        "ad_rejected", f"Объявление «{ad.get('name') or ad['ad_id']}» отклонено",
        body=summary or "Причина не указана.", severity="warn", act_id=act_id,
        dedup_key=f"ad_rejected:{ad['ad_id']}:{summary[:40]}"))


# Meta отдаёт insights примерно за 37 месяцев — дальше запрашивать бесполезно.
MAX_HISTORY_DAYS = 1100


def _spend_window(account: dict[str, Any], since: str,
                  until: str) -> tuple[str, str]:
    """Окно статистики для одного кабинета.

    Обычно это скользящее окно прогона. Но кабинет, по которому фактов ещё
    нет, надо засеять историей: иначе кабинет с открутом в прошлом месяце
    показывает ноль, потому что в последние семь дней он молчал. Дальше
    работает обычное окно — история второй раз не перезабирается.
    """
    if store.has_spend_rows(account["act_id"]):
        return since, until
    floor = (date.today() - timedelta(days=MAX_HISTORY_DAYS)).isoformat()
    created = str(account.get("created_time") or "")[:10]
    start = created if created >= floor else floor
    return min(since, start or floor), until


def _remember_platform_user(identity_id: str, platform_user_id: str,
                            platform_name: str = "") -> None:
    """Запомнить, чей это профиль, и назвать личку по-человечески.

    Метку при добавлении ставит бот — «tg 14.08 11:25», по ней невозможно
    понять, какой это профиль. Как только Meta назвала владельца, служебная
    метка заменяется на имя. Метку, заданную человеком, не трогаем.
    """
    if not platform_user_id:
        return
    with store.connect() as conn:
        conn.execute(store._q(
            "UPDATE identities SET platform_user_id = ? WHERE id = ?"),
            (platform_user_id, identity_id))
        if platform_name:
            conn.execute(store._q("""
                UPDATE identities SET label = ?
                 WHERE id = ? AND (label IS NULL OR label = '' OR label LIKE 'tg %')
            """), (platform_name[:64], identity_id))


def _upsert_business(biz: dict[str, Any]) -> None:
    stamp = store.now()
    with store.connect() as conn:
        conn.execute(store._q("""
            INSERT INTO businesses (bm_id, name, verification_status,
                                    is_disabled_for_integrity, created_time,
                                    first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (bm_id) DO UPDATE SET
                name = ?, verification_status = ?,
                is_disabled_for_integrity = ?, last_seen_at = ?
        """), (biz["bm_id"], biz["name"], biz["verification_status"],
               biz["is_disabled_for_integrity"], biz.get("created_time"),
               stamp, stamp,
               biz["name"], biz["verification_status"],
               biz["is_disabled_for_integrity"], stamp))


def _remember_access(act_id: str, identity_id: str) -> None:
    """Какая личка видит кабинет. Не то же самое, что владение: у агентского
    кабинета доступ бывает у нескольких личек, а владелец всегда один."""
    stamp = store.now()
    with store.connect() as conn:
        conn.execute(store._q("""
            INSERT INTO ad_account_access (act_id, identity_id, first_seen_at,
                                           last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (act_id, identity_id) DO UPDATE SET last_seen_at = ?
        """), (act_id, identity_id, stamp, stamp, stamp))


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
def run(days: int = 7, since: str | None = None, until: str | None = None,
        registry_only: bool = False, tx_days: int = 45) -> dict[str, Any]:
    store.init_db()
    if since and until:
        window = (since, until)
    else:
        window = platforms.rolling_window(days)

    identities = store.active_identities()
    if not identities:
        log("Активных личек нет — собирать нечего.")
        return {"identities": 0, "accounts": 0, "spend_rows": 0}

    kind = "registry" if registry_only else f"window:{window[0]}..{window[1]}"
    run_id = start_run(kind)
    log(f"Прогон #{run_id}: личек {len(identities)}, окно {window[0]}..{window[1]}"
        + (" (только реестр)" if registry_only else ""))

    totals = {"identities": len(identities), "accounts": 0, "spend_rows": 0,
              "geo_rows": 0, "tx_rows": 0, "campaign_rows": 0, "ad_rows": 0,
              "breakdown_rows": 0, "campaigns": 0, "ads": 0, "rejects": 0,
              "alerts": 0, "errors": 0, "dead": 0,
              "card_payments": 0, "cards_seen": 0}
    try:
        for entry in identities:
            stats = collect_identity(entry, run_id, window[0], window[1],
                                     registry_only, tx_days)
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + value
    except Exception as exc:  # noqa: BLE001 — прогон логируем целиком
        finish_run(run_id, "failed", {**totals, "fatal": str(exc)})
        traceback.print_exc()
        raise

    with store.connect() as conn:
        cur = conn.execute(store._q(
            "SELECT COUNT(*) AS n FROM identities WHERE status = 'needs_reconnect'"))
        totals["dead"] = int(store.rows(cur)[0]["n"])

    # Стареющие токены. Долгоживущий токен Facebook живёт около 60 дней и
    # повторным обменом не продлевается — значит единственный способ не
    # ловить падение врасплох: предупредить заранее и перевыпустить по
    # календарю. Отдельно от проверки живости: тут личика ещё работает.
    try:
        aging = alerts.identity_aging()
        if aging:
            log(f"  [ТОКЕНЫ] предупреждений о сроке: {aging}")
    except Exception as exc:  # noqa: BLE001 — напоминалка не роняет прогон
        log(f"  [ТОКЕНЫ] проверка срока не удалась: {exc}")

    # Карты — источник, не зависящий от личек: свой ключ, свои лимиты,
    # поэтому мёртвый мультитокен Meta сбору платежей не мешает.
    try:
        for key, value in collect_cards(run_id, tx_days).items():
            totals[key] = totals.get(key, 0) + value
    except Exception as exc:  # noqa: BLE001 — сверка не должна ронять прогон
        log(f"  [КАРТЫ] прогон не удался: {exc}")

    # Данные восстановления из таблицы — по имени сессии. Не зависит от FB.
    try:
        res = sheet.sync_recovery()
        totals["recovery_rows"] = res["written"]
        log(f"  [ТАБЛИЦА] восстановление: {res['written']} записей")
    except Exception as exc:  # noqa: BLE001
        log(f"  [ТАБЛИЦА] синк не удался: {exc}")

    # Keitaro: справочник кампаний трекера и сопоставление со ссылками.
    # После сбора объявлений и до итогов — ссылки к этому моменту уже в базе.
    if keitaro.configured():
        try:
            fetched = keitaro.fetch_campaigns()
            store.replace_keitaro_campaigns(
                [keitaro.campaign_row(c) for c in fetched])
            matcher = keitaro.Matcher(store.keitaro_campaigns())
            hit = 0
            ads_seen = store.ads_with_links()
            for ad in ads_seen:
                found, how = matcher.match(ad.get("link") or "")
                store.set_ad_keitaro(ad["ad_id"], ad.get("act_id", ""),
                                     ad.get("campaign_id", ""),
                                     ad.get("link") or "",
                                     (found or {}).get("kt_id", 0), how)
                hit += 1 if found else 0
            totals["keitaro_campaigns"] = len(fetched)
            totals["keitaro_matched"] = hit
            log(f"  [KEITARO] кампаний {len(fetched)}, сопоставлено "
                f"объявлений {hit} из {len(ads_seen)}")
        except keitaro.KeitaroError as exc:
            log(f"  [KEITARO] {exc}")
        except Exception as exc:  # noqa: BLE001 — трекер не повод ронять сбор
            log(f"  [KEITARO] сопоставление не удалось: {exc}")

    # Автоочистка комментариев — только там, где владелец её включил.
    try:
        cleaned = 0
        for page in store.autoclean_pages():
            if page:
                cleaned += clean_page_comments(page, delete=True)["deleted"]
        if cleaned:
            totals["comments_deleted"] = cleaned
            log(f"  [КОММЕНТЫ] удалено автоматически: {cleaned}")
    except Exception as exc:  # noqa: BLE001
        log(f"  [КОММЕНТЫ] автоочистка не удалась: {exc}")

    finish_run(run_id, "ok", totals)
    log(f"Готово: кабинетов {totals['accounts']}, строк спенда "
        f"{totals['spend_rows']}, гео {totals['geo_rows']}, "
        f"крео-фактов {totals['ad_rows']}, разбивок {totals['breakdown_rows']}, "
        f"кампаний {totals['campaigns']}, объявлений {totals['ads']}, "
        f"реджектов {totals['rejects']}, транзакций {totals['tx_rows']}, "
        f"алертов {totals['alerts']}, ошибок {totals['errors']}, "
        f"личек к реконнекту {totals['dead']}")

    # Спенд без владельца — операционный долг, о нём надо знать сразу.
    orphans = store.unassigned_spend(window[0], window[1])
    if orphans:
        log(f"ВНИМАНИЕ: кабинетов со спендом без владельца — {len(orphans)}. "
            f"Назначьте: python3 manage.py owner set <act_id> <маркетолог>")
    return totals


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Сбор данных Admetrics")
    p.add_argument("--days", type=int, default=7,
                   help="ширина скользящего окна (по умолчанию 7)")
    p.add_argument("--since", help="начало периода YYYY-MM-DD")
    p.add_argument("--until", help="конец периода YYYY-MM-DD")
    p.add_argument("--registry-only", action="store_true",
                   help="только реестр кабинетов и снапшоты, без статистики")
    p.add_argument("--tx-days", type=int, default=45,
                   help="глубина выборки транзакций")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if bool(args.since) != bool(args.until):
        raise SystemExit("--since и --until указываются вместе")
    run(days=args.days, since=args.since, until=args.until,
        registry_only=args.registry_only, tx_days=args.tx_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
