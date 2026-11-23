"""
Правила алертов и сверка с картами.

Два источника событий:

  1. **Диф состояния кабинета.** «Последнее известное состояние» — это строка
     в `ad_accounts`. Коллектор перед записью свежих данных сравнивает их с ней
     и получает события. Отдельная таблица под внутрисуточные снапшоты не нужна:
     реестр сам и есть текущее состояние, а `ad_account_snapshots` остаётся
     суточной историей для отчётов.

  2. **Сверка FB ↔ MyBroCards.** В порог идут **Settled + Pending, без
     комиссий**. Pending — уже захолдированные деньги, они почти всегда
     доезжают до Settled, просто финальный статус приходит через часы; без
     них сверка «за сегодня» всегда показывала бы недостачу. Дублирования
     при переходе не возникает: ключ таблицы — `payment_id`, платёж
     перезаписывается на месте, а уехавший в Declined сам выпадает из суммы.

     Отклонённые попытки (их много, обычно $2 `card-blocked`) деньгами
     не являются, а комиссия MyBroCards — расход инфраструктуры, а не
     рекламы. И то и другое показывается отдельными строками, но в 5%
     не входит.

Дедупликация обязательна: коллектор ходит каждые несколько минут, и без ключа
один бан прилетал бы владельцу на каждом прогоне.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import os

from . import store
from .graph import ACCOUNT_STATUS_RU, DISABLE_REASON_RU

ZERO = Decimal("0")

# Статусы, при которых кабинет считается нерабочим.
DEAD_STATUSES = {2, 3, 7, 8, 100, 101}

# Порог сверки: и относительный, и абсолютный. Без абсолютного на кабинете
# с открутом в $20 расхождение в 5% — это доллар, и алерт только шумит.
RECON_PCT = Decimal(os.environ.get("ADMETRICS_RECON_PCT", "5"))
RECON_MIN_ABS = Decimal(os.environ.get("ADMETRICS_RECON_MIN_ABS", "20"))


# --------------------------------------------------------------------------- #
# Запись и выдача
# --------------------------------------------------------------------------- #
def raise_alert(rule: str, title: str, body: str = "", severity: str = "warn",
                act_id: str | None = None, identity_id: str | None = None,
                marketer_id: str | None = None,
                payload: dict[str, Any] | None = None,
                dedup_key: str | None = None) -> bool:
    """Записать алерт. Возвращает False, если такой уже был (дедуп)."""
    key = dedup_key or f"{rule}:{act_id or identity_id or marketer_id}"
    with store.connect() as conn:
        existing = store.rows(conn.execute(
            store._q("SELECT id FROM alerts WHERE dedup_key = ?"), (key,)))
        if existing:
            return False
        conn.execute(store._q("""
            INSERT INTO alerts (rule, severity, act_id, identity_id, marketer_id,
                                title, body, payload_json, created_at, dedup_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """), (rule, severity, act_id, identity_id, marketer_id, title, body,
               json.dumps(payload or {}, ensure_ascii=False, default=str),
               store.now(), key))
    return True


def undelivered() -> list[dict[str, Any]]:
    """Алерты, которые ещё не ушли в Telegram, с адресатом.

    Адресат — владелец кабинета на дату алерта. Если владельца нет, письмо
    уходит только админам: иначе алерт потеряется совсем.
    """
    with store.connect() as conn:
        return store.rows(conn.execute("""
            SELECT a.*, COALESCE(a.marketer_id, o.marketer_id) AS recipient_id,
                   m.tg_id AS recipient_tg, m.name AS recipient_name
              FROM alerts a
              LEFT JOIN ad_account_owner o
                     ON o.act_id = a.act_id AND o.until IS NULL
              LEFT JOIN marketers m
                     ON m.id = COALESCE(a.marketer_id, o.marketer_id)
             WHERE a.delivered_at IS NULL
             ORDER BY a.created_at
        """))


def mark_delivered(alert_id: Any) -> None:
    with store.connect() as conn:
        conn.execute(store._q("UPDATE alerts SET delivered_at = ? WHERE id = ?"),
                     (store.now(), alert_id))


def recent(limit: int = 20) -> list[dict[str, Any]]:
    with store.connect() as conn:
        return store.rows(conn.execute(store._q("""
            SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?
        """), (limit,)))


# --------------------------------------------------------------------------- #
# Диф состояния кабинета
# --------------------------------------------------------------------------- #
def account_changes(before: dict[str, Any] | None,
                    after: dict[str, Any]) -> list[dict[str, Any]]:
    """События из сравнения «было → стало» по одному кабинету.

    `before is None` — кабинет видим впервые. Тогда алертим только если он
    сразу приехал мёртвым: новый забаненный кабинет — это новость, а новый
    живой — обычное дело.
    """
    out: list[dict[str, Any]] = []
    act_id = after.get("act_id")
    name = after.get("name") or act_id

    new_status = after.get("account_status")
    old_status = (before or {}).get("account_status")
    status_word = ACCOUNT_STATUS_RU.get(new_status, str(new_status))
    reason_word = DISABLE_REASON_RU.get(after.get("disable_reason"), "")

    if new_status in DEAD_STATUSES and new_status != old_status:
        out.append({
            "rule": "account_dead", "severity": "bad", "act_id": act_id,
            "title": f"Кабинет «{name}» — {status_word}",
            "body": (f"Причина: {reason_word}." if reason_word else
                     "Причина не указана."),
            "dedup_key": f"account_dead:{act_id}:{new_status}",
            "payload": {"was": old_status, "now": new_status},
        })
    elif (before is not None and old_status in DEAD_STATUSES
            and new_status == 1):
        out.append({
            "rule": "account_alive", "severity": "info", "act_id": act_id,
            "title": f"Кабинет «{name}» снова активен",
            "dedup_key": f"account_alive:{act_id}:{store.now()[:13]}",
        })

    if before is not None:
        old_dsl = store.dec(before.get("daily_spend_limit"))
        new_dsl = store.dec(after.get("daily_spend_limit"))
        if old_dsl != new_dsl and (old_dsl or new_dsl):
            up = new_dsl > old_dsl
            out.append({
                "rule": "dsl_up" if up else "dsl_down",
                "severity": "info" if up else "warn", "act_id": act_id,
                "title": (f"Кабинет «{name}»: лимит "
                          f"{'подняли' if up else 'порезали'} "
                          f"{old_dsl:,.2f} → {new_dsl:,.2f}"),
                "dedup_key": f"dsl:{act_id}:{old_dsl}:{new_dsl}",
                "payload": {"was": str(old_dsl), "now": str(new_dsl)},
            })

        old_card = (before.get("funding_last4") or "").strip()
        new_card = (after.get("funding_last4") or "").strip()
        if old_card != new_card:
            out.append({
                "rule": "card_changed", "severity": "warn", "act_id": act_id,
                "title": f"Кабинет «{name}»: сменилась карта",
                "body": f"Было •{old_card or '—'}, стало •{new_card or '—'}.",
                "dedup_key": f"card:{act_id}:{old_card}:{new_card}",
            })

    return out


def emit_account_changes(before: dict[str, Any] | None,
                         after: dict[str, Any]) -> int:
    written = 0
    for event in account_changes(before, after):
        if raise_alert(**event):
            written += 1
    return written


def _identity_lifetime(identity_id: str) -> str:
    """«, прожила 6 ч» — или пусто, если сравнивать не с чем.

    Берём последний успешный сбор: между ним и этим моментом токен работал.
    Точности до минуты здесь не нужно — важен порядок величины, он и решает,
    смотреть ли на срок токена или на гигиену работы с аккаунтом.
    """
    try:
        last_ok = store.identity_last_ok(identity_id)
    except Exception:  # noqa: BLE001 — диагностика не должна ронять алерт
        return ""
    if not last_ok:
        return ""
    # store.now() пишет время в UTC с зоной. Вычитать из него наивное
    # datetime.now() нельзя — Python бросит TypeError, и подсказка тихо
    # пропадёт из уведомления, хотя всё остальное будет работать.
    from datetime import datetime, timezone
    try:
        started = datetime.fromisoformat(str(last_ok).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
    except (TypeError, ValueError):
        return ""
    if hours < 0:
        return ""
    if hours < 48:
        return f", прожила {hours:.0f} ч"
    return f", прожила {hours / 24:.0f} дн"


def identity_dead(identity_id: str, marketer_id: str, label: str,
                  reason: str) -> bool:
    # Сколько личика прожила с последнего успешного сбора. Это половина
    # диагноза: токен, проживший два месяца, просто истёк по сроку, а
    # проживший шесть часов убит сессией — чекпойнтом, сменой пароля или
    # входом с другого адреса. Без этой цифры обе смерти выглядят одинаково,
    # и разбор «почему токены не живут» упирается в догадки.
    lived = _identity_lifetime(identity_id)
    return raise_alert(
        "identity_dead", f"Личка «{label}» отвалилась", severity="bad",
        body=f"{reason}{lived}. Пока она мертва, статистика по её кабинетам "
             f"не собирается — переподключите мультитокен.",
        identity_id=identity_id, marketer_id=marketer_id,
        # Дневной бакет: пока личика мертва, напоминание приходит раз в сутки,
        # а не один раз навсегда (иначе «отвалилась» легко пропустить) и не
        # каждые 10 минут (иначе спам). Одно сообщение в день — до починки.
        dedup_key=f"identity_dead:{identity_id}:{store.now()[:10]}")


# Долгоживущий токен Facebook живёт около 60 дней, и продлить его повторным
# обменом нельзя — нужен новый вход в аккаунт. Значит единственный способ не
# ловить смерть врасплох: предупредить заранее и перевыпустить по календарю.
#
# Порог 50 дней, а не 58: между «пора» и «сделали» у живой команды проходит
# неделя, и предупреждение впритык означало бы то же самое падение, только
# с уведомлением. Второе напоминание — на 57-й день, когда времени почти нет.
TOKEN_WARN_DAYS = 50
TOKEN_URGENT_DAYS = 57


def identity_aging(limit_days: int = TOKEN_WARN_DAYS) -> int:
    """Предупредить о личиках, чей токен вот-вот истечёт по сроку.

    Считаем от подключения, а не от последнего успешного сбора: сбор
    происходит каждые несколько минут и обнулял бы счётчик всегда. Момент
    подключения — это момент выпуска токена, от него и идёт шестьдесят дней.

    Точную дату истечения знает только приложение, выпустившее токен
    (`/debug_token`). Пока токены выпускает чужое приложение, этого знания у
    нас нет, и шестьдесят дней — оценка. Оценка, сказанная заранее, всё равно
    полезнее внезапного падения в три часа ночи.
    """
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    written = 0
    for ident in store.list_identities():
        if ident.get("status") in ("disabled", "revoked", "needs_reconnect"):
            continue          # мёртвую и выключенную торопить незачем
        started = _as_utc(ident.get("connected_at") or ident.get("created_at"))
        if started is None:
            continue
        days = (now_utc - started).days
        if days < limit_days:
            continue
        urgent = days >= TOKEN_URGENT_DAYS
        label = ident.get("label") or ident.get("id")
        left = max(0, 60 - days)
        written += int(raise_alert(
            "identity_aging",
            f"Личка «{label}»: токену {days} дн",
            body=(f"Долгоживущий токен Facebook живёт около 60 дней — этому "
                  f"осталось примерно {left}. Продлить существующий нельзя, "
                  f"нужен новый вход в аккаунт и новый мультитокен. "
                  f"Перевыпустите заранее, пока сбор ещё идёт."),
            severity="bad" if urgent else "warn",
            identity_id=ident.get("id"),
            marketer_id=ident.get("marketer_id") or "",
            # Раз в сутки и отдельно для двух порогов: мягкое напоминание не
            # должно глушить срочное, когда до истечения осталось три дня.
            dedup_key=(f"identity_aging:{ident.get('id')}:"
                       f"{'urgent' if urgent else 'warn'}:{store.now()[:10]}")))
    return written


def _as_utc(stamp: Any):
    """Строка времени из базы → datetime с зоной. Иначе — None."""
    if not stamp:
        return None
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Сверка FB ↔ MyBroCards
# --------------------------------------------------------------------------- #
def reconcile(since: str, until: str) -> dict[str, Any]:
    """Сравнить спенд FB со списаниями с карт за период.

    В порог идут только Settled без комиссий. Отклонённые попытки и комиссии
    считаются отдельно и показываются как справка — они реальны, но рекламным
    расходом не являются, и включать их в 5% значит гарантировать ложные
    срабатывания.

    Связка карта → кабинет идёт по `funding_last4`. Если у кабинета карта
    не проставлена (Meta не отдала `funding_source_details`), он попадает
    в `unmatched_accounts`, а не тихо исчезает из сверки.
    """
    with store.connect() as conn:
        fb = store.rows(conn.execute(store._q("""
            SELECT a.act_id, a.name, a.funding_last4, a.currency,
                   o.marketer_id, m.name AS marketer_name,
                   COALESCE(SUM(s.spend), 0) AS spend
              FROM ad_accounts a
              LEFT JOIN spend_daily s
                     ON s.act_id = a.act_id AND s.date >= ? AND s.date <= ?
              LEFT JOIN ad_account_owner o
                     ON o.act_id = a.act_id AND o.until IS NULL
              LEFT JOIN marketers m ON m.id = o.marketer_id
             GROUP BY a.act_id, a.name, a.funding_last4, a.currency,
                      o.marketer_id, m.name
        """), (since, until)))

        # В зачёт идут Settled И Pending. Pending — это уже захолдированные
        # деньги: платёж прошёл авторизацию и почти всегда доедет до Settled,
        # просто финальный статус приходит через часы. Без него сверка «за
        # сегодня» всегда показывала бы недостачу — это и был лаг расчётов.
        #
        # Дублирования не возникает: ключ таблицы — `payment_id`, поэтому при
        # переходе Pending → Settled строка перезаписывается на месте, а не
        # добавляется второй. И если платёж уйдёт в Declined, он так же
        # автоматически выпадет из суммы при следующем пересчёте.
        cards = store.rows(conn.execute(store._q("""
            SELECT card_id, card_last4, MAX(user_email) AS user_email, currency,
                   SUM(CASE WHEN LOWER(state) IN ('settled', 'pending')
                            THEN amount ELSE 0 END) AS charged,
                   SUM(CASE WHEN LOWER(state) = 'settled'
                            THEN amount ELSE 0 END) AS settled,
                   SUM(CASE WHEN LOWER(state) = 'pending'
                            THEN amount ELSE 0 END) AS pending,
                   SUM(CASE WHEN LOWER(state) NOT IN ('settled', 'pending')
                            THEN amount ELSE 0 END) AS declined,
                   SUM(fee) AS fees,
                   COUNT(*) AS payments,
                   COUNT(DISTINCT card_id) AS cards,
                   SUM(CASE WHEN LOWER(state) NOT IN ('settled', 'pending')
                            THEN 1 ELSE 0 END) AS declines
              FROM card_payments
             WHERE date >= ? AND date <= ?
             GROUP BY card_id, card_last4, currency
        """), (since, until)))

    # Кандидаты по (last4, валюта): на коллизии их несколько.
    from collections import defaultdict
    by_last4: dict[tuple, list[dict]] = defaultdict(list)
    by_id: dict[str, dict] = {}
    for c in cards:
        by_last4[(c["card_last4"], (c["currency"] or "").upper())].append(c)
        by_id[c["card_id"]] = c

    # Карта → маркетолог по email создателя (машинный факт из MyBroCard).
    email_to_mid = {}
    for m in store.list_marketers():
        login = str(m.get("login") or "").strip().lower()
        if login:
            email_to_mid[login] = m["id"]

    used: set[str] = set()
    rows_out, mismatched, unmatched = [], [], []

    for account in fb:
        last4 = (account["funding_last4"] or "").strip()
        currency = (account["currency"] or "").upper()
        spend = store.dec(account["spend"])
        candidates = by_last4.get((last4, currency), []) if last4 else []

        # Одна карта — берём её. Несколько — точную выбирает матчер по
        # машинным фактам (пары транзакций, поведение), а не по подписи.
        card, resolved_by = None, ""
        if len(candidates) == 1:
            card, resolved_by = candidates[0], "last4"
        elif len(candidates) > 1:
            m = store.match_card_for_account(account["act_id"], since, until)
            if m.get("card"):
                card = by_id.get(m["card"]["card_id"])
                resolved_by = m.get("method", "behavior")
        if card:
            used.add(card["card_id"])

        charged = store.dec(card["charged"]) if card else ZERO
        # Конфликт владельца: карту создал один маркетолог, а кабинет
        # принадлежит другому. Машинный сигнал «привязали чужую карту».
        card_mid = email_to_mid.get((card["user_email"] or "").lower()) if card else None
        owner_conflict = bool(card and card_mid and account["marketer_id"]
                              and card_mid != account["marketer_id"])
        row = {
            "ambiguous_card": len(candidates) > 1,
            "cards_behind_last4": len(candidates),
            "resolved_by": resolved_by,
            "card_owner_email": card["user_email"] if card else "",
            "owner_conflict": owner_conflict,
            "act_id": account["act_id"], "name": account["name"],
            "marketer_id": account["marketer_id"],
            "marketer_name": account["marketer_name"] or "Без владельца",
            "last4": last4, "currency": currency,
            "fb_spend": spend,
            # В порог идёт `card_charged` = Settled + Pending.
            "card_charged": charged,
            "card_settled": store.dec(card["settled"]) if card else ZERO,
            "card_pending": store.dec(card["pending"]) if card else ZERO,
            "card_declined": store.dec(card["declined"]) if card else ZERO,
            "card_fees": store.dec(card["fees"]) if card else ZERO,
            "declines": int(card["declines"]) if card else 0,
        }
        diff = charged - spend
        base = max(spend, charged)
        row["diff"] = diff
        row["diff_pct"] = (abs(diff) / base * 100) if base else ZERO

        if not last4:
            unmatched.append(row)
        elif not card and spend > ZERO:
            row["reason"] = "нет платежей по этой карте за период"
            unmatched.append(row)
        else:
            rows_out.append(row)
            if row["diff_pct"] > RECON_PCT and abs(diff) >= RECON_MIN_ABS:
                mismatched.append(row)

    orphan_cards = [c for cid, c in by_id.items() if cid not in used
                    and store.dec(c["charged"]) > ZERO]

    return {
        "since": since, "until": until,
        "threshold_pct": RECON_PCT, "threshold_abs": RECON_MIN_ABS,
        "rows": sorted(rows_out, key=lambda r: -r["fb_spend"]),
        "mismatched": sorted(mismatched, key=lambda r: -abs(r["diff"])),
        "unmatched_accounts": unmatched,
        "orphan_cards": orphan_cards,
        "total_fb": sum((r["fb_spend"] for r in rows_out), ZERO),
        "total_charged": sum((r["card_charged"] for r in rows_out), ZERO),
        "total_settled": sum((r["card_settled"] for r in rows_out), ZERO),
        "total_pending": sum((r["card_pending"] for r in rows_out), ZERO),
        "total_fees": sum((r["card_fees"] for r in rows_out), ZERO),
        "total_declined": sum((r["card_declined"] for r in rows_out), ZERO),
    }


def emit_reconciliation(since: str, until: str) -> int:
    """Поднять алерты по расхождениям. Ключ дедупа включает период —
    за новый период тот же кабинет проалертит снова, и это правильно."""
    result = reconcile(since, until)
    written = 0
    for row in result["mismatched"]:
        ok = raise_alert(
            "recon_mismatch", severity="warn", act_id=row["act_id"],
            marketer_id=row["marketer_id"],
            title=(f"Расхождение по «{row['name']}»: "
                   f"{row['diff_pct']:.1f}%"),
            body=(f"FB: {row['fb_spend']:,.2f} {row['currency']} · "
                  f"карта •{row['last4']}: {row['card_charged']:,.2f} "
                  f"(в т.ч. pending {row['card_pending']:,.2f}) · "
                  f"разница {row['diff']:+,.2f}"),
            payload={k: str(v) for k, v in row.items()},
            dedup_key=f"recon:{row['act_id']}:{since}:{until}")
        written += int(ok)
    return written
