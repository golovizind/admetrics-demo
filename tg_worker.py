#!/usr/bin/env python3
"""
Воркер технических аккаунтов Telegram.

Один процесс держит подключёнными все рабочие аккаунты и делает четыре вещи:

  1. ловит коды входа (от «Telegram» 777000, «Verification Codes» 489000
     и от ботов, если в тексте есть код) и кладёт их в `tg_codes`;
  2. следит за здоровьем: `get_me` раз в полчаса, @SpamBot раз в сутки;
  3. раз в 6 часов переписывает список каналов и групп, где аккаунт админ;
  4. выполняет задачи из `tg_jobs`, которые ставит панель: проверка,
     синхронизация, публикация (в том числе отложенная).

Отдельный процесс, а не поток бота: бот синхронный (`requests`), а Telethon —
asyncio; и главное — сессия должна жить ровно в одном месте. Панель сама
в Telegram от имени аккаунта не ходит, она ставит задачу сюда.

Запуск:  python3 tg_worker.py
"""

from __future__ import annotations

import asyncio
import hashlib
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from core import store, tgclient, tgstore

TICK = 5                    # с, как часто смотреть в очередь
CHECK_EVERY = 30 * 60       # get_me
SPAM_EVERY = 24 * 3600      # @SpamBot
SYNC_EVERY = 6 * 3600       # каналы
RECONNECT_PAUSE = 120       # не долбить мёртвый прокси каждые 5 секунд


def log(*args: Any) -> None:
    print(datetime.now().strftime("%H:%M:%S"), *args, flush=True)


def _age(stamp: str | None) -> float:
    if not stamp:
        return float("inf")
    try:
        t = datetime.fromisoformat(str(stamp))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return float("inf")


def _fp(acc: dict[str, Any]) -> str:
    """Отпечаток сессии и прокси: сменились — клиента надо пересоздать."""
    return hashlib.sha256((acc["session"] + "|" + acc["proxy"]).encode()).hexdigest()


class Holder:
    def __init__(self, acc: dict[str, Any]) -> None:
        self.id = acc["id"]
        self.fp = _fp(acc)
        self.client = tgclient.make_client(acc["session"], acc["proxy"])
        self.ready = False


class Worker:
    def __init__(self) -> None:
        self.holders: dict[str, Holder] = {}
        self.failed_at: dict[str, float] = {}
        self.stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    async def _apply_error(self, aid: str, exc: BaseException) -> None:
        status, note = tgclient.classify(exc)
        if status:
            tgstore.set_status(aid, status, note)
            if status in tgstore.BAD and status != "spamblock":
                await self.drop(aid)
        log(aid, "ошибка:", note)

    async def drop(self, aid: str) -> None:
        h = self.holders.pop(aid, None)
        if h:
            try:
                await h.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def connect(self, acc: dict[str, Any]) -> Holder | None:
        aid = acc["id"]
        h = Holder(acc)
        try:
            await h.client.connect()
            if not await h.client.is_user_authorized():
                tgstore.set_status(aid, "revoked", "Сессия не авторизована — "
                                   "нужен повторный вход.")
                await h.client.disconnect()
                return None
            me = await h.client.get_me()
            self._register(h)
            # Наполнить кэш сущностей: без него публикация по id канала
            # не найдёт access_hash.
            await h.client.get_dialogs(limit=200)
            h.ready = True
            prev = (tgstore.get_account(aid) or {}).get("status")
            if prev in ("unknown", "error"):
                tgstore.set_status(aid, "ok", f"на связи как {me.first_name or ''}")
            log(aid, "подключён")
            return h
        except Exception as exc:  # noqa: BLE001
            self.failed_at[aid] = time.monotonic()
            await self._apply_error(aid, exc)
            try:
                await h.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return None

    def _register(self, h: Holder) -> None:
        from telethon import events
        aid = h.id

        @h.client.on(events.NewMessage(incoming=True))
        async def on_message(event: Any) -> None:  # noqa: ANN401
            try:
                if not event.is_private:
                    return
                sid = event.sender_id or 0
                text = event.raw_text or ""
                if sid in tgclient.CODE_SENDERS:
                    sender = "Telegram" if sid == 777000 else "Verification Codes"
                else:
                    sender_ent = await event.get_sender()
                    if not getattr(sender_ent, "bot", False):
                        return
                    if not tgstore.extract_code(text):
                        return
                    sender = "@" + (getattr(sender_ent, "username", "") or str(sid))
                tgstore.add_code(aid, sender, text)
                log(aid, "пришёл код от", sender)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    # ------------------------------------------------------------------ #
    async def sync_holders(self) -> None:
        wanted = {a["id"]: a for a in tgstore.worker_accounts()}
        for aid in list(self.holders):
            if aid not in wanted or self.holders[aid].fp != _fp(wanted[aid]):
                await self.drop(aid)
                log(aid, "отключён (снят с работы или сменилась сессия/прокси)")
        for aid, acc in wanted.items():
            h = self.holders.get(aid)
            if h and h.client.is_connected():
                continue
            if h:
                await self.drop(aid)
            if time.monotonic() - self.failed_at.get(aid, -1e9) < RECONNECT_PAUSE:
                continue
            nh = await self.connect(acc)
            if nh:
                self.holders[aid] = nh

    async def routine(self) -> None:
        """Плановые проверки по каждому подключённому аккаунту."""
        accs = {a["id"]: a for a in tgstore.list_accounts()}
        for aid, h in list(self.holders.items()):
            a = accs.get(aid) or {}
            try:
                if _age(a.get("last_check_at")) > CHECK_EVERY:
                    await self.do_check(aid, h)
                if _age(a.get("last_spamcheck_at")) > SPAM_EVERY:
                    await self.do_spamcheck(aid, h)
                if _age(a.get("last_sync_at")) > SYNC_EVERY:
                    await self.do_sync(aid, h)
            except Exception as exc:  # noqa: BLE001
                await self._apply_error(aid, exc)

    async def do_check(self, aid: str, h: Holder) -> dict[str, Any]:
        me = await h.client.get_me()
        if me is None:
            raise RuntimeError("get_me вернул пусто")
        cur = (tgstore.get_account(aid) or {}).get("status")
        # Успешный get_me не снимает спамблок: его снимает только @SpamBot.
        if cur != "spamblock":
            tgstore.set_status(aid, "ok", "на связи")
        else:
            tgstore.mark(aid, "last_check_at")
        return tgclient.me_dict(me)

    async def do_spamcheck(self, aid: str, h: Holder) -> dict[str, Any]:
        status, text = await tgclient.spamcheck(h.client)
        tgstore.mark(aid, "last_spamcheck_at")
        if status:
            tgstore.set_status(aid, status, f"@SpamBot: {text}")
        return {"status": status or "не распознан", "answer": text}

    async def do_sync(self, aid: str, h: Holder) -> dict[str, Any]:
        items = await tgclient.list_admin_chats(h.client)
        n = tgstore.replace_channels(aid, items)
        tgstore.mark(aid, "last_sync_at")
        return {"channels": n}

    # ------------------------------------------------------------------ #
    async def run_job(self, job: dict[str, Any]) -> None:
        aid, kind, jid = job["account_id"], job["kind"], job["id"]
        h = self.holders.get(aid)
        if h is None and kind == "check":
            # Проверка — единственное, ради чего стоит подключиться вне
            # очереди: человек как раз хочет знать, жив ли аккаунт.
            self.failed_at.pop(aid, None)
            acc = next((a for a in tgstore.worker_accounts() if a["id"] == aid), None)
            if acc:
                h = await self.connect(acc)
                if h:
                    self.holders[aid] = h
        if h is None:
            a = tgstore.get_account(aid) or {}
            tgstore.finish_job(jid, "failed", error=(
                f"аккаунт не на связи: {a.get('status_text', 'нет данных')}"
                + (f" — {a['status_note']}" if a.get("status_note") else "")))
            return
        try:
            if kind == "check":
                res = await self.do_check(aid, h)
            elif kind == "spamcheck":
                res = await self.do_spamcheck(aid, h)
            elif kind == "sync":
                res = await self.do_sync(aid, h)
            elif kind == "post":
                res = await tgclient.publish(h.client, job["payload"]["chat_id"],
                                             job["payload"])
            else:
                raise ValueError(f"неизвестная задача {kind}")
            tgstore.finish_job(jid, "done", res)
            log(aid, kind, "готово")
        except Exception as exc:  # noqa: BLE001
            status, note = tgclient.classify(exc)
            # Обрыв связи посреди публикации — неизвестность, а не отказ:
            # пост мог уйти. Отказ сервера (RPC-ошибка) — точно не ушёл.
            outcome = "unknown" if (kind == "post" and tgclient.is_network(exc)) \
                else "failed"
            if outcome == "unknown":
                note = ("Связь оборвалась во время отправки — неизвестно, вышел ли "
                        "пост. Проверьте канал, прежде чем повторять.")
            tgstore.finish_job(jid, outcome, error=note)
            # Статус аккаунта трогаем только при поломке самого аккаунта.
            # «Нет прав писать в этот канал» — про канал, а не про аккаунт:
            # пометить его ошибочным значило бы снять его со всех остальных.
            if status in tgstore.BAD:
                await self._apply_error(aid, exc)
            else:
                log(aid, kind, "не вышло:", note)

    # ------------------------------------------------------------------ #
    async def main(self) -> None:
        err = tgclient.configured()
        if err:
            log("Не запущен:", err)
            # Не выходим с ошибкой: systemd поднимал бы процесс каждые 5 с.
            await self.stop.wait()
            return
        n = tgstore.requeue_stale()
        if n:
            log("разобрано зависших задач:", n)
        last_purge = 0.0
        last_routine = 0.0
        while not self.stop.is_set():
            try:
                await self.sync_holders()
                for job in tgstore.claim_jobs():
                    await self.run_job(job)
                if time.monotonic() - last_routine > 60:
                    await self.routine()
                    last_routine = time.monotonic()
                if time.monotonic() - last_purge > 3600:
                    tgstore.purge_codes()
                    last_purge = time.monotonic()
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                traceback.print_exc()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
        for aid in list(self.holders):
            await self.drop(aid)


def main() -> int:
    store.init_db()
    w = Worker()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, w.stop.set)
        except NotImplementedError:
            pass
    log("воркер Telegram-аккаунтов запущен")
    loop.run_until_complete(w.main())
    return 0


if __name__ == "__main__":
    sys.exit(main())
