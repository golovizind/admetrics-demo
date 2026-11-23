"""
Единая схема Admetrics × SpendTool.

Три слоя, как в согласованной концепции:

  1. ДОСТУПЫ  — маркетолог, его ФБ-личики (мультитокены), история статусов;
  2. АКТИВЫ   — BM, рекламные кабинеты, владение, суточные снапшоты, карты;
  3. ФАКТЫ    — открут по дням/кампаниям/объявлениям/странам, транзакции FB,
                списания с карт, курсы валют.

Главное архитектурное решение зашито в ключи: **владелец не входит в первичный
ключ факта**. Спенд кабинета за день — объективное число и не зависит от того,
чей токен его прочитал. Атрибуция маркетологу живёт отдельно, в `ad_account_owner`,
с историей. Иначе шаренный кабинет либо перетирает спенд (как сейчас в обеих
заготовках), либо задваивает его (если добавить владельца в PK).

Базовая валюта — рубль. Ни одна таблица фактов не хранит пересчитанную сумму:
`*_base` считается на чтении джойном к `fx_rates`, чтобы не было второго
источника правды, который разойдётся при уточнении курса.

Работает на двух движках: Postgres (прод) и SQLite (локально), как в admetrics.
"""

from __future__ import annotations

import os
from typing import Any

# Базовая валюта отчётности. Решение по проекту — рубль.
BASE_CURRENCY = os.environ.get("ADMETRICS_BASE_CURRENCY", "USD").upper()

# Отдельная схема Postgres под наши таблицы. Нужна не для красоты: база может
# быть не пустой — например, там уже лежит `marketers` от прошлой версии с
# другими колонками. Тогда `CREATE TABLE IF NOT EXISTS` молча ничего не делает,
# а следующий же внешний ключ падает с «column id does not exist». Своя схема
# убирает этот класс поломок целиком: чужие таблицы в `public` не видны нашим.
PG_SCHEMA = os.environ.get("ADMETRICS_PG_SCHEMA", "admetrics")


def types_for(pg: bool) -> dict[str, str]:
    """Типы, различающиеся между движками.

    Деньги в Postgres — NUMERIC(18,4): для финансового контроля бинарная
    плавающая точка не годится, накопленная погрешность на суммах за цикл
    вылезает в копейках, а сверка с картами идёт до копейки. В SQLite
    честного decimal нет, поэтому REAL — но SQLite здесь только для локальной
    разработки и тестов.
    """
    return {
        "blob": "BYTEA" if pg else "BLOB",
        "money": "NUMERIC(18,4)" if pg else "REAL",
        "rate": "NUMERIC(18,8)" if pg else "REAL",
        "serial": "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT",
    }


def tables(pg: bool) -> list[str]:
    t = types_for(pg)
    blob, money, rate, serial = t["blob"], t["money"], t["rate"], t["serial"]
    # В SQLite схем нет — префикс пустой.
    sch = f"{PG_SCHEMA}." if pg else ""

    return [
        # ------------------------------------------------------------------ #
        # СЛОЙ 1 — ДОСТУПЫ
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}marketers (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            login         TEXT UNIQUE,
            password_hash TEXT,
            tg_id         TEXT UNIQUE,
            role          TEXT NOT NULL DEFAULT 'marketer',
            team          TEXT DEFAULT '',
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL
        )
        """,
        # Личка = один мультитокен FBTOOL. У маркетолога их много — это и есть
        # то, чего не позволяла старая схема с одним секретом на человека.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}identities (
            id                TEXT PRIMARY KEY,
            marketer_id       TEXT NOT NULL REFERENCES {sch}marketers(id),
            platform          TEXT NOT NULL DEFAULT 'fb',
            label             TEXT DEFAULT '',
            platform_user_id  TEXT DEFAULT '',
            secret_encrypted  {blob},
            secret_kind       TEXT DEFAULT 'session',
            session_name      TEXT DEFAULT '',
            proxy_encrypted   {blob},
            app_id            TEXT DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'unknown',
            last_error        TEXT DEFAULT '',
            last_ok_at        TEXT,
            last_checked_at   TEXT,
            -- Когда в личку положили ТЕКУЩИЙ мультитокен. Отличается от
            -- created_at: личка живёт годами, а токен в ней меняют. Срок
            -- жизни токена считается отсюда, иначе после каждой замены
            -- отсчёт вёлся бы от заведения личики и всегда был бы неверным.
            connected_at      TEXT,
            created_at        TEXT NOT NULL
        )
        """,
        # История статусов — отвечает на «когда именно умерла личка» и даёт
        # среднее время жизни токена. Одной перезаписываемой колонки для этого мало.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}identity_status_log (
            id             {serial},
            identity_id    TEXT NOT NULL REFERENCES {sch}identities(id),
            status         TEXT NOT NULL,
            error_code     INTEGER,
            error_subcode  INTEGER,
            message        TEXT DEFAULT '',
            at             TEXT NOT NULL
        )
        """,

        # ------------------------------------------------------------------ #
        # СЛОЙ 2 — АКТИВЫ
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}businesses (
            bm_id                     TEXT PRIMARY KEY,
            name                      TEXT DEFAULT '',
            verification_status       TEXT DEFAULT '',
            is_disabled_for_integrity INTEGER DEFAULT 0,
            credit_available          {money},
            credit_currency           TEXT DEFAULT '',
            created_time              TEXT,
            first_seen_at             TEXT NOT NULL,
            last_seen_at              TEXT NOT NULL
        )
        """,
        # Реестр активов. Кабинет попадает сюда фактом существования, а не
        # фактом трат — иначе забаненный до открута кабинет невидим для учёта.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ad_accounts (
            act_id             TEXT PRIMARY KEY,
            platform           TEXT NOT NULL DEFAULT 'fb',
            bm_id              TEXT,
            name               TEXT DEFAULT '',
            currency           TEXT DEFAULT '',
            timezone_name      TEXT DEFAULT '',
            account_status     INTEGER,
            status_label       TEXT DEFAULT '',
            disable_reason     INTEGER,
            disable_label      TEXT DEFAULT '',
            amount_spent       {money},
            balance            {money},
            spend_cap          {money},
            daily_spend_limit  {money},
            funding_type       TEXT DEFAULT '',
            funding_last4      TEXT DEFAULT '',
            funding_display    TEXT DEFAULT '',
            created_time       TEXT,
            next_bill_date     TEXT DEFAULT '',
            first_seen_at      TEXT NOT NULL,
            last_seen_at       TEXT NOT NULL
        )
        """,
        # ВЛАДЕНИЕ — вынесено из фактов. Интервальное, с историей: отчёт за
        # прошлый цикл должен считаться по владельцу на тот момент, а не по
        # текущему. until IS NULL — владение открыто.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ad_account_owner (
            act_id      TEXT NOT NULL,
            marketer_id TEXT NOT NULL REFERENCES {sch}marketers(id),
            since       TEXT NOT NULL,
            until       TEXT,
            note        TEXT DEFAULT '',
            PRIMARY KEY (act_id, since)
        )
        """,
        # Технический доступ: какая личка видит кабинет. Не то же самое, что
        # владение — у агентского кабинета доступ бывает у нескольких личек.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ad_account_access (
            act_id      TEXT NOT NULL,
            identity_id TEXT NOT NULL REFERENCES {sch}identities(id),
            role        TEXT DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL,
            PRIMARY KEY (act_id, identity_id)
        )
        """,
        # Суточный срез — источник всех алертов. Алерт это всегда диф между
        # вчерашним и сегодняшним снапшотом.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ad_account_snapshots (
            act_id            TEXT NOT NULL,
            snapshot_date     TEXT NOT NULL,
            account_status    INTEGER,
            disable_reason    INTEGER,
            daily_spend_limit {money},
            spend_cap         {money},
            balance           {money},
            amount_spent      {money},
            funding_last4     TEXT DEFAULT '',
            captured_at       TEXT NOT NULL,
            PRIMARY KEY (act_id, snapshot_date)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}cards (
            card_id     TEXT PRIMARY KEY,
            last4       TEXT DEFAULT '',
            title       TEXT DEFAULT '',
            marketer_id TEXT REFERENCES {sch}marketers(id),
            status      TEXT DEFAULT '',
            balance     {money},
            currency    TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """,

        # ------------------------------------------------------------------ #
        # СЛОЙ 3 — ФАКТЫ. Владельца в ключах нет — см. заголовок модуля.
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}spend_daily (
            date        TEXT NOT NULL,
            platform    TEXT NOT NULL,
            act_id      TEXT NOT NULL,
            currency    TEXT NOT NULL DEFAULT '',
            spend       {money} NOT NULL DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            reach       INTEGER DEFAULT 0,
            clicks      INTEGER DEFAULT 0,
            link_clicks INTEGER DEFAULT 0,
            leads       {money} DEFAULT 0,
            purchases   {money} DEFAULT 0,
            revenue     {money} DEFAULT 0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (date, platform, act_id)
        )
        """,
        # Гео — первоклассное измерение (иерархия фаундера маркетолог→цикл→гео),
        # поэтому берётся из breakdowns=country, то есть из фактической
        # доставки, а не из разбора названия кампании.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}spend_country_daily (
            date        TEXT NOT NULL,
            platform    TEXT NOT NULL,
            act_id      TEXT NOT NULL,
            country     TEXT NOT NULL,
            currency    TEXT NOT NULL DEFAULT '',
            spend       {money} NOT NULL DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            clicks      INTEGER DEFAULT 0,
            leads       {money} DEFAULT 0,
            purchases   {money} DEFAULT 0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (date, platform, act_id, country)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}spend_campaign_daily (
            date        TEXT NOT NULL,
            platform    TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            act_id      TEXT NOT NULL,
            campaign_name TEXT DEFAULT '',
            objective   TEXT DEFAULT '',
            currency    TEXT NOT NULL DEFAULT '',
            spend       {money} NOT NULL DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            reach       INTEGER DEFAULT 0,
            clicks      INTEGER DEFAULT 0,
            link_clicks INTEGER DEFAULT 0,
            leads       {money} DEFAULT 0,
            purchases   {money} DEFAULT 0,
            revenue     {money} DEFAULT 0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (date, platform, campaign_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ad_daily (
            date        TEXT NOT NULL,
            platform    TEXT NOT NULL,
            ad_id       TEXT NOT NULL,
            act_id      TEXT NOT NULL,
            campaign_id TEXT DEFAULT '',
            country     TEXT DEFAULT '',
            currency    TEXT NOT NULL DEFAULT '',
            spend       {money} NOT NULL DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            clicks      INTEGER DEFAULT 0,
            link_clicks INTEGER DEFAULT 0,
            leads       {money} DEFAULT 0,
            purchases   {money} DEFAULT 0,
            revenue     {money} DEFAULT 0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (date, platform, ad_id)
        )
        """,
        # Адсеты: таргетинг и бюджет уровня группы объявлений.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}adsets (
            adset_id          TEXT PRIMARY KEY,
            act_id            TEXT NOT NULL,
            campaign_id       TEXT DEFAULT '',
            name              TEXT DEFAULT '',
            effective_status  TEXT DEFAULT '',
            -- `status` — то, чем управляем мы; `effective_status` Meta считает
            -- сама, с оглядкой на кампанию и кабинет. Для «остановлен ли адсет
            -- нами» нужно первое: иначе нашу паузу не отличить от чужой.
            status            TEXT DEFAULT '',
            optimization_goal TEXT DEFAULT '',
            daily_budget      {money},
            lifetime_budget   {money},
            age_min           INTEGER,
            age_max           INTEGER,
            genders           TEXT DEFAULT '',
            countries         TEXT DEFAULT '',
            platforms         TEXT DEFAULT '',
            interests         TEXT DEFAULT '',
            custom_audiences  TEXT DEFAULT '',
            excluded          TEXT DEFAULT '',
            devices           TEXT DEFAULT '',
            locales           TEXT DEFAULT '',
            billing_event     TEXT DEFAULT '',
            updated_at        TEXT NOT NULL
        )
        """,
        # Разрезы открута, кроме страны: плейсмент, устройство, возраст, пол.
        # Ключ включает `kind` и `value`, поэтому новый разрез не требует
        # новой таблицы — только новой строки.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}spend_breakdown_daily (
            date        TEXT NOT NULL,
            platform    TEXT NOT NULL,
            act_id      TEXT NOT NULL,
            kind        TEXT NOT NULL,
            value       TEXT NOT NULL DEFAULT '',
            currency    TEXT NOT NULL DEFAULT '',
            spend       {money} NOT NULL DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            clicks      INTEGER DEFAULT 0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (date, platform, act_id, kind, value)
        )
        """,
        # Page-токены для модерации комментариев. Токен страницы живёт
        # отдельно от мультитокена и протухает по своим правилам.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}page_tokens (
            page_id         TEXT PRIMARY KEY,
            name            TEXT DEFAULT '',
            token_encrypted {blob},
            identity_id     TEXT,
            marketer_id     TEXT,
            can_moderate    INTEGER DEFAULT 0,
            auto_clean      INTEGER DEFAULT 0,
            last_cleaned_at TEXT,
            last_deleted    INTEGER DEFAULT 0,
            updated_at      TEXT NOT NULL
        )
        """,
        # Прокси отдельно от личек: один прокси обслуживает несколько личек,
        # а `fingerprint` не даёт завести один и тот же дважды.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}proxies (
            id            TEXT PRIMARY KEY,
            -- Чей прокси. Пул общий на всю базу, но видеть в нём чужое
            -- маркетологу незачем: в строке прокси логин и пароль, и даже
            -- выбор чужого id по подсказке означал бы, что его учётка
            -- уходит работать под чужой личикой.
            marketer_id   TEXT DEFAULT '',
            label         TEXT DEFAULT '',
            hint          TEXT DEFAULT '',
            fingerprint   TEXT UNIQUE,
            url_encrypted {blob},
            created_at    TEXT NOT NULL
        )
        """,
        # Данные для восстановления аккаунта: у кого фарм, ссылка, логин.
        # Секрет шифруется тем же ключом, что и мультитокены.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}account_recovery (
            session_name     TEXT PRIMARY KEY,
            farmer           TEXT DEFAULT '',
            account_name     TEXT DEFAULT '',
            fb_link          TEXT DEFAULT '',
            login            TEXT DEFAULT '',
            secret_encrypted {blob},
            updated_at       TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}keitaro_campaigns (
            kt_id       INTEGER PRIMARY KEY,
            alias       TEXT DEFAULT '',
            name        TEXT DEFAULT '',
            grp         TEXT DEFAULT '',
            domain      TEXT DEFAULT '',
            host        TEXT DEFAULT '',
            state       TEXT DEFAULT '',
            token       TEXT DEFAULT '',
            traffic_source_id INTEGER DEFAULT 0,
            updated_at  TEXT NOT NULL
        )
        """,
        # Связь «объявление → кампания трекера». Отдельной таблицей, а не
        # колонкой в ads_meta: связь пересчитывается при каждом сборе и имеет
        # собственную надёжность (`matched_by`), а креатив живёт своей жизнью.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ad_keitaro (
            ad_id       TEXT PRIMARY KEY,
            act_id      TEXT DEFAULT '',
            campaign_id TEXT DEFAULT '',
            link        TEXT DEFAULT '',
            kt_id       INTEGER DEFAULT 0,
            matched_by  TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ads_meta (
            platform   TEXT NOT NULL,
            ad_id      TEXT NOT NULL,
            title      TEXT DEFAULT '',
            body       TEXT DEFAULT '',
            preview_url TEXT DEFAULT '',
            thumb_url  TEXT DEFAULT '',
            creative_id TEXT DEFAULT '',
            object_type TEXT DEFAULT '',
            cta        TEXT DEFAULT '',
            link_url   TEXT DEFAULT '',
            permalink  TEXT DEFAULT '',
            instagram_url TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (platform, ad_id)
        )
        """,
        # Фактические списания FB — то, что сверяется с картами.
        # Это НЕ insights: insights даёт оценку по показам, биллинг — деньги.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}fb_transactions (
            tx_id          TEXT PRIMARY KEY,
            act_id         TEXT NOT NULL,
            time           TEXT NOT NULL,
            charge_type    TEXT DEFAULT '',
            status         TEXT DEFAULT '',
            payment_option TEXT DEFAULT '',
            currency       TEXT NOT NULL DEFAULT '',
            amount         {money} NOT NULL DEFAULT 0,
            tax_amount     {money} DEFAULT 0,
            updated_at     TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}card_transactions (
            id              TEXT PRIMARY KEY,
            card_id         TEXT NOT NULL,
            time            TEXT NOT NULL,
            currency        TEXT NOT NULL DEFAULT '',
            amount          {money} NOT NULL DEFAULT 0,
            merchant        TEXT DEFAULT '',
            status          TEXT DEFAULT '',
            matched_fb_tx_id TEXT,
            match_confidence TEXT DEFAULT '',
            updated_at      TEXT NOT NULL
        )
        """,
        # ------------------------------------------------------------------ #
        # СЛОЙ 2б — КОНТРОЛЬ ДОСТУПА И ИСТОРИЯ ИЗМЕНЕНИЙ
        # ------------------------------------------------------------------ #
        # Кто ещё сидит в кабинете. Meta отдаёт это отдельным ребром, и без
        # него «кабинет закреплён за М16» — утверждение о нашей базе, а не
        # о реальности: доступ мог остаться у прежнего человека или у чужого.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}account_users (
            act_id      TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            name        TEXT DEFAULT '',
            tasks       TEXT DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL,
            PRIMARY KEY (act_id, user_id)
        )
        """,

        # Журнал изменений кабинета: кто срезал бюджет, кто добавил человека,
        # когда кабинет отключили. Это ровно то, ради чего система называется
        # контролем: снапшот показывает «стало», а журнал — «кто сделал».
        f"""
        CREATE TABLE IF NOT EXISTS {sch}account_activities (
            id          TEXT PRIMARY KEY,
            act_id      TEXT NOT NULL,
            event_time  TEXT NOT NULL,
            event_type  TEXT DEFAULT '',
            event_label TEXT DEFAULT '',
            actor_id    TEXT DEFAULT '',
            actor_name  TEXT DEFAULT '',
            object_id   TEXT DEFAULT '',
            object_name TEXT DEFAULT '',
            extra_json  TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """,

        # Что токен реально умеет. Половина «сбор ничего не собрал» —
        # это не поломка, а отсутствующее разрешение, и увидеть его можно
        # только спросив у Meta напрямую.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}token_permissions (
            identity_id TEXT NOT NULL,
            permission  TEXT NOT NULL,
            status      TEXT DEFAULT '',
            checked_at  TEXT NOT NULL,
            PRIMARY KEY (identity_id, permission)
        )
        """,

        # Пиксели кабинета. Объясняют пустые лиды и покупки: нет пикселя —
        # неоткуда взяться конверсиям, и это не ошибка сбора.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}pixels (
            pixel_id       TEXT PRIMARY KEY,
            act_id         TEXT DEFAULT '',
            bm_id          TEXT DEFAULT '',
            name           TEXT DEFAULT '',
            last_fired_at  TEXT,
            updated_at     TEXT NOT NULL
        )
        """,

                # Кто имеет доступ к BM. Доступ к бизнесу сильнее доступа к кабинету:
        # он дотягивается до всех кабинетов внутри, включая те, которых нет
        # в нашем реестре. `kind` различает работающего человека,
        # приглашённого и системного пользователя — приглашение опасно тем,
        # что превратится в доступ само, без нашего участия.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}business_users (
            bm_id       TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            kind        TEXT DEFAULT '',
            name        TEXT DEFAULT '',
            email       TEXT DEFAULT '',
            role        TEXT DEFAULT '',
            title       TEXT DEFAULT '',
            two_factor  TEXT DEFAULT '',
            expires_at  TEXT DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL,
            PRIMARY KEY (bm_id, user_id)
        )
        """,

        # Страницы бизнеса. Клиентская страница отличается от своей не
        # косметически: её владелец может забрать в любой момент, и реклама
        # встанет вместе с ней.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}business_pages (
            page_id     TEXT NOT NULL,
            bm_id       TEXT NOT NULL,
            kind        TEXT DEFAULT '',
            name        TEXT DEFAULT '',
            link        TEXT DEFAULT '',
            category    TEXT DEFAULT '',
            verification_status TEXT DEFAULT '',
            fan_count   INTEGER DEFAULT 0,
            is_published INTEGER DEFAULT 1,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (bm_id, page_id)
        )
        """,

        # Автоправила кабинета. Правило меняет бюджеты и статусы само —
        # журнал изменений покажет результат, но автором назовёт робота.
        # Условие и расписание хранятся уже переведёнными, сырой spec
        # рядом в JSON: перевод может устареть, исходник — нет.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ad_rules (
            rule_id     TEXT PRIMARY KEY,
            act_id      TEXT NOT NULL,
            name        TEXT DEFAULT '',
            status      TEXT DEFAULT '',
            action      TEXT DEFAULT '',
            condition_text TEXT DEFAULT '',
            schedule    TEXT DEFAULT '',
            created_time TEXT,
            updated_time TEXT,
            spec_json   TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """,

        # Журнал НАШИХ действий. Отдельно от `account_activities`: там журнал
        # со слов Meta, здесь — что было сделано из Admetrics, кем именно
        # и что было ДО. Без «до» разбор «кто уронил открут» упирается в то,
        # что прежнее значение уже негде взять: Meta хранит только текущее.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}control_actions (
            id          {serial},
            at          TEXT NOT NULL,
            actor_id    TEXT DEFAULT '',
            actor_name  TEXT DEFAULT '',
            actor_role  TEXT DEFAULT '',
            act_id      TEXT DEFAULT '',
            target_kind TEXT DEFAULT '',
            target_id   TEXT DEFAULT '',
            target_name TEXT DEFAULT '',
            op          TEXT DEFAULT '',
            before_val  TEXT DEFAULT '',
            after_val   TEXT DEFAULT '',
            status      TEXT DEFAULT '',
            error       TEXT DEFAULT ''
        )
        """,

        # Ключи внешнего API. Ключ хранится ХЕШЕМ, а не как есть: база
        # уезжает в бэкапы и на рабочие машины, и ключ из бэкапа работает
        # ровно так же, как живой. Показываем его ровно один раз — в момент
        # выпуска; потерявший ключ выпускает новый, а не «смотрит старый».
        f"""
        CREATE TABLE IF NOT EXISTS {sch}api_keys (
            id          TEXT PRIMARY KEY,
            name        TEXT DEFAULT '',
            key_hash    TEXT NOT NULL,
            prefix      TEXT DEFAULT '',
            marketer_id TEXT DEFAULT '',
            scope       TEXT DEFAULT 'read',
            created_at  TEXT NOT NULL,
            created_by  TEXT DEFAULT '',
            last_used_at TEXT,
            last_ip     TEXT DEFAULT '',
            calls       INTEGER DEFAULT 0,
            revoked_at  TEXT
        )
        """,

        # Курсы к базовой валюте. Таблица ПЛОТНАЯ: строка на каждый день,
        # включая выходные (курс переносится вперёд). Так пересчёт в отчётах —
        # обычный JOIN, а не коррелированный подзапрос «последний курс до даты».
        f"""
        CREATE TABLE IF NOT EXISTS {sch}fx_rates (
            date        TEXT NOT NULL,
            currency    TEXT NOT NULL,
            rate_to_base {rate} NOT NULL,
            source      TEXT DEFAULT '',
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (date, currency)
        )
        """,

        # ------------------------------------------------------------------ #
        # СУЩНОСТИ КАМПАНИЙ И ОБЪЯВЛЕНИЙ — состояние, а не суточный факт.
        # Нужны боту: «вся информация по активным кампаниям» и реджекты.
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}campaigns (
            campaign_id      TEXT PRIMARY KEY,
            act_id           TEXT NOT NULL,
            name             TEXT DEFAULT '',
            status           TEXT DEFAULT '',
            effective_status TEXT DEFAULT '',
            objective        TEXT DEFAULT '',
            buying_type      TEXT DEFAULT '',
            daily_budget     {money},
            lifetime_budget  {money},
            budget_remaining {money},
            bid_strategy     TEXT DEFAULT '',
            issues_json      TEXT DEFAULT '',
            created_time     TEXT,
            updated_at       TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}ads (
            ad_id            TEXT PRIMARY KEY,
            act_id           TEXT NOT NULL,
            campaign_id      TEXT DEFAULT '',
            adset_id         TEXT DEFAULT '',
            name             TEXT DEFAULT '',
            effective_status TEXT DEFAULT '',
            issues_json      TEXT DEFAULT '',
            story_id         TEXT DEFAULT '',
            updated_at       TEXT NOT NULL
        )
        """,

        # ------------------------------------------------------------------ #
        # MyBroCards — платёж в том виде, в каком его отдаёт их API.
        # Отклонённые и комиссии хранятся, но в сверку не попадают:
        # порог считается по Settled без fee (решение по проекту).
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}card_payments (
            payment_id   TEXT PRIMARY KEY,
            card_id      TEXT DEFAULT '',
            card_last4   TEXT DEFAULT '',
            card_bin     TEXT DEFAULT '',
            card_title   TEXT DEFAULT '',
            user_email   TEXT DEFAULT '',
            user_name    TEXT DEFAULT '',
            user_id      TEXT DEFAULT '',
            team         TEXT DEFAULT '',
            marketer_id  TEXT,
            merchant     TEXT DEFAULT '',
            descriptor   TEXT DEFAULT '',
            state        TEXT DEFAULT '',
            decline      TEXT DEFAULT '',
            is_micro     INTEGER DEFAULT 0,
            currency     TEXT DEFAULT '',
            amount       {money} NOT NULL DEFAULT 0,
            fee          {money} DEFAULT 0,
            date         TEXT NOT NULL,
            date_final   TEXT,
            updated_at   TEXT NOT NULL
        )
        """,

        # ------------------------------------------------------------------ #
        # Привязка Telegram: одноразовый код вместо пароля.
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}tg_link_codes (
            code        TEXT PRIMARY KEY,
            marketer_id TEXT NOT NULL REFERENCES {sch}marketers(id),
            created_at  TEXT NOT NULL,
            used_at     TEXT,
            used_by_tg  TEXT
        )
        """,

        # ------------------------------------------------------------------ #
        # Заказы в SMM-панели (smmlaba). Храним у себя, чтобы вести историю и
        # подтягивать статус — панель отдаёт его только по id заказа.
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}smm_orders (
            order_id     TEXT PRIMARY KEY,
            service_id   TEXT DEFAULT '',
            service_name TEXT DEFAULT '',
            category     TEXT DEFAULT '',
            link         TEXT DEFAULT '',
            quantity     INTEGER DEFAULT 0,
            charge       {money} DEFAULT 0,
            currency     TEXT DEFAULT '',
            start_count  INTEGER DEFAULT 0,
            remains      INTEGER DEFAULT 0,
            status       TEXT DEFAULT '',
            marketer_id  TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
        """,

        # ------------------------------------------------------------------ #
        # Мелкие служебные значения: докуда дотянута история платежей и т.п.
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}app_kv (
            k          TEXT PRIMARY KEY,
            v          TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,

        # ------------------------------------------------------------------ #
        # СЛУЖЕБНОЕ — очередь сбора и алерты
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}collection_runs (
            id          {serial},
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            kind        TEXT DEFAULT '',
            status      TEXT DEFAULT 'running',
            stats_json  TEXT DEFAULT '{{}}'
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}collection_tasks (
            id          {serial},
            run_id      INTEGER,
            identity_id TEXT,
            act_id      TEXT,
            kind        TEXT NOT NULL,
            since       TEXT,
            until       TEXT,
            status      TEXT NOT NULL DEFAULT 'pending',
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}alerts (
            id          {serial},
            rule        TEXT NOT NULL,
            severity    TEXT NOT NULL DEFAULT 'info',
            act_id      TEXT,
            identity_id TEXT,
            marketer_id TEXT,
            title       TEXT NOT NULL,
            body        TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{{}}',
            created_at  TEXT NOT NULL,
            delivered_at TEXT,
            dedup_key   TEXT UNIQUE
        )
        """,

        # ------------------------------------------------------------------ #
        # ТЕХНИЧЕСКИЕ АККАУНТЫ TELEGRAM
        # Отдельный контур от ФБ-личек: другой протокол (MTProto), другой
        # жизненный цикл, другие активы (каналы). Общее — шифрование секретов
        # тем же ключом и прокси из того же пула.
        # ------------------------------------------------------------------ #
        f"""
        CREATE TABLE IF NOT EXISTS {sch}tg_accounts (
            id                 TEXT PRIMARY KEY,
            label              TEXT DEFAULT '',
            phone              TEXT DEFAULT '',
            tg_user_id         TEXT DEFAULT '',
            username           TEXT DEFAULT '',
            first_name         TEXT DEFAULT '',
            -- StringSession Telethon. Это ключ авторизации целиком: кто его
            -- прочитал, тот вошёл в аккаунт. Поэтому только шифрованно.
            session_encrypted  {blob},
            proxy_encrypted    {blob},
            twofa_encrypted    {blob},
            -- Незавершённый вход: временная сессия + phone_code_hash.
            login_encrypted    {blob},
            -- new | code_sent | password_needed | ok | spamblock | frozen |
            -- revoked | banned | error | unknown
            status             TEXT NOT NULL DEFAULT 'new',
            status_note        TEXT DEFAULT '',
            enabled            INTEGER NOT NULL DEFAULT 1,
            last_ok_at         TEXT,
            last_check_at      TEXT,
            last_spamcheck_at  TEXT,
            last_sync_at       TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {sch}tg_status_log (
            id          {serial},
            account_id  TEXT NOT NULL,
            status      TEXT NOT NULL,
            note        TEXT DEFAULT '',
            at          TEXT NOT NULL
        )
        """,
        # Каналы и группы, где аккаунт — владелец или админ. Переписывается
        # целиком на каждой синхронизации: пропавшая строка = права сняли.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}tg_channels (
            account_id    TEXT NOT NULL,
            chat_id       TEXT NOT NULL,
            title         TEXT DEFAULT '',
            username      TEXT DEFAULT '',
            kind          TEXT DEFAULT '',
            role          TEXT DEFAULT '',
            can_post      INTEGER NOT NULL DEFAULT 0,
            can_add_admins INTEGER NOT NULL DEFAULT 0,
            rights_json   TEXT DEFAULT '{{}}',
            participants  INTEGER,
            first_seen_at TEXT NOT NULL,
            seen_at       TEXT NOT NULL,
            PRIMARY KEY (account_id, chat_id)
        )
        """,
        # Коды входа, пришедшие на аккаунты. Текст шифруется, живёт сутки.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}tg_codes (
            id              {serial},
            account_id      TEXT NOT NULL,
            sender          TEXT DEFAULT '',
            code            TEXT DEFAULT '',
            text_encrypted  {blob},
            received_at     TEXT NOT NULL,
            viewed_at       TEXT,
            viewed_by       TEXT DEFAULT ''
        )
        """,
        # Очередь действий для воркера и одновременно журнал: кто, что,
        # чем кончилось. Панель сама в Telegram не ходит — одна сессия
        # не должна работать из двух процессов одновременно.
        f"""
        CREATE TABLE IF NOT EXISTS {sch}tg_jobs (
            id           {serial},
            account_id   TEXT NOT NULL,
            kind         TEXT NOT NULL,
            payload_json TEXT DEFAULT '{{}}',
            status       TEXT NOT NULL DEFAULT 'pending',
            result_json  TEXT DEFAULT '{{}}',
            error        TEXT DEFAULT '',
            created_by   TEXT DEFAULT '',
            run_at       TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            started_at   TEXT,
            finished_at  TEXT
        )
        """,
    ]


def indexes(pg: bool) -> list[str]:
    sch = f"{PG_SCHEMA}." if pg else ""
    return [
        f"CREATE INDEX IF NOT EXISTS idx_identities_marketer ON {sch}identities(marketer_id, status)",
        f"CREATE INDEX IF NOT EXISTS idx_idlog_identity ON {sch}identity_status_log(identity_id, at)",
        f"CREATE INDEX IF NOT EXISTS idx_accounts_bm ON {sch}ad_accounts(bm_id)",
        f"CREATE INDEX IF NOT EXISTS idx_accounts_card ON {sch}ad_accounts(funding_last4)",
        f"CREATE INDEX IF NOT EXISTS idx_owner_marketer ON {sch}ad_account_owner(marketer_id, since)",
        f"CREATE INDEX IF NOT EXISTS idx_owner_act ON {sch}ad_account_owner(act_id, since, until)",
        f"CREATE INDEX IF NOT EXISTS idx_snap_date ON {sch}ad_account_snapshots(snapshot_date)",
        f"CREATE INDEX IF NOT EXISTS idx_spend_daily_date ON {sch}spend_daily(date)",
        f"CREATE INDEX IF NOT EXISTS idx_spend_country_date ON {sch}spend_country_daily(date, country)",
        f"CREATE INDEX IF NOT EXISTS idx_spend_country_act ON {sch}spend_country_daily(act_id, date)",
        f"CREATE INDEX IF NOT EXISTS idx_spend_campaign_act ON {sch}spend_campaign_daily(act_id, date)",
        f"CREATE INDEX IF NOT EXISTS idx_ad_daily_act ON {sch}ad_daily(act_id, date)",
        f"CREATE INDEX IF NOT EXISTS idx_fbtx_act ON {sch}fb_transactions(act_id, time)",
        f"CREATE INDEX IF NOT EXISTS idx_cardtx_card ON {sch}card_transactions(card_id, time)",
        f"CREATE INDEX IF NOT EXISTS idx_cardtx_match ON {sch}card_transactions(matched_fb_tx_id)",
        f"CREATE INDEX IF NOT EXISTS idx_tasks_status ON {sch}collection_tasks(status, kind)",
        f"CREATE INDEX IF NOT EXISTS idx_alerts_created ON {sch}alerts(created_at)",
        f"CREATE INDEX IF NOT EXISTS idx_alerts_undelivered ON {sch}alerts(delivered_at)",
        f"CREATE INDEX IF NOT EXISTS idx_tg_jobs_pending ON {sch}tg_jobs(status, run_at)",
        f"CREATE INDEX IF NOT EXISTS idx_tg_codes_acc ON {sch}tg_codes(account_id, received_at)",
        f"CREATE INDEX IF NOT EXISTS idx_campaigns_act ON {sch}campaigns(act_id, effective_status)",
        f"CREATE INDEX IF NOT EXISTS idx_ads_act ON {sch}ads(act_id, effective_status)",
        f"CREATE INDEX IF NOT EXISTS idx_ads_campaign ON {sch}ads(campaign_id)",
        f"CREATE INDEX IF NOT EXISTS idx_cardpay_date ON {sch}card_payments(date)",
        f"CREATE INDEX IF NOT EXISTS idx_cardpay_last4 ON {sch}card_payments(card_last4, date)",
        f"CREATE INDEX IF NOT EXISTS idx_cardpay_marketer ON {sch}card_payments(marketer_id, date)",
        # Журнал читается всегда «по кабинету и свежести» — без индекса на
        # десятках тысяч строк это перебор всей таблицы на каждый экран.
        f"CREATE INDEX IF NOT EXISTS idx_activities_act ON {sch}account_activities(act_id, event_time)",
        f"CREATE INDEX IF NOT EXISTS idx_pixels_act ON {sch}pixels(act_id)",
        f"CREATE INDEX IF NOT EXISTS idx_pixels_bm ON {sch}pixels(bm_id)",
        f"CREATE INDEX IF NOT EXISTS idx_rules_act ON {sch}ad_rules(act_id, status)",
        f"CREATE INDEX IF NOT EXISTS idx_actions_act ON {sch}control_actions(act_id, at)",
        f"CREATE INDEX IF NOT EXISTS idx_kt_host ON {sch}keitaro_campaigns(host, alias)",
        f"CREATE INDEX IF NOT EXISTS idx_adkt_camp ON {sch}ad_keitaro(campaign_id)",
        f"CREATE INDEX IF NOT EXISTS idx_adkt_kt ON {sch}ad_keitaro(kt_id)",
    ]


def declared_columns(pg: bool) -> dict[str, list[tuple[str, str]]]:
    """Что схема объявляет: {таблица: [(колонка, объявление)]}.

    Разбирается тот же текст, который исполняется, — второго списка колонок
    в коде нет и быть не должно: он бы разошёлся с первым в первый же день.
    """
    import re
    out: dict[str, list[tuple[str, str]]] = {}
    for stmt in tables(pg):
        m = re.search(r"CREATE TABLE IF NOT EXISTS\s+(\S+)\s*\((.*)\)\s*$",
                      stmt, re.S)
        if not m:
            continue
        name = m.group(1).split(".")[-1]
        cols: list[tuple[str, str]] = []
        for line in m.group(2).splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(
                    ("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK", "--")):
                continue
            cols.append((line.split()[0], line))
        out[name] = cols
    return out


def existing_columns(conn: Any, table: str, pg: bool) -> set[str]:
    if pg:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (PG_SCHEMA, table)).fetchall()
    else:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    out = set()
    for r in rows:
        # sqlite отдаёт кортеж (cid, name, ...), Postgres — одну колонку.
        out.add(str(r[1] if not pg and len(r) > 1 else r[0]))
    return out


def ensure_columns(conn: Any, pg: bool) -> list[str]:
    """Дописать колонки, появившиеся в схеме после создания таблицы.

    `CREATE TABLE IF NOT EXISTS` на существующей таблице не делает НИЧЕГО —
    новая колонка в объявлении просто не доезжает до базы, а код начинает
    падать на `no such column` уже в проде. Здесь недостающие добавляются
    по одной.

    NOT NULL и ключи при добавлении снимаются намеренно: в непустую таблицу
    колонку без значения по умолчанию добавить нельзя, а придумывать значение
    задним числом — это уже миграция данных, а не схемы, и делать её молча
    нельзя.
    """
    added: list[str] = []
    prefix = f"{PG_SCHEMA}." if pg else ""
    for table, cols in declared_columns(pg).items():
        try:
            have = existing_columns(conn, table, pg)
        except Exception:  # noqa: BLE001 — таблицы ещё нет, её создаст create_all
            continue
        if not have:
            continue
        for name, decl in cols:
            if name in have:
                continue
            body = decl.split(None, 1)[1] if " " in decl else "TEXT"
            for drop in ("NOT NULL", "PRIMARY KEY", "UNIQUE"):
                body = body.replace(drop, "")
            body = body.split("REFERENCES")[0].strip()
            conn.execute(f"ALTER TABLE {prefix}{table} ADD COLUMN {name} {body}")
            added.append(f"{table}.{name}")
    return added


def create_all(conn: Any, pg: bool) -> None:
    """Создать схему, таблицы и индексы, дописать новые колонки. Идемпотентно."""
    if pg:
        # Сначала пространство имён, иначе первая же CREATE TABLE уйдёт
        # в public и столкнётся с чужими таблицами.
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA}")
    added = ensure_columns(conn, pg)
    for stmt in tables(pg):
        conn.execute(stmt)
    for stmt in indexes(pg):
        conn.execute(stmt)
    if added:
        import sys
        print("Схема дополнена: " + ", ".join(added), file=sys.stderr)
