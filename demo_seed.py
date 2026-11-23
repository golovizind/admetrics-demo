#!/usr/bin/env python3
"""
Демо-данные для Admetrics: вымышленная команда, кабинеты, открут, карты.

Нужен, чтобы показать панель без доступа к живым Meta, MyBroCards и Keitaro.
Все имена, кабинеты, карты и суммы сгенерированы. Генерация детерминированная
(фиксированный seed), даты отсчитываются от сегодняшнего дня, поэтому демо
всегда выглядит «свежим».

    python3 demo_seed.py            # пересоздать admetrics.sqlite3
    python3 demo_seed.py --days 60  # длиннее история
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

if not os.environ.get("ADMETRICS_KEY"):
    from cryptography.fernet import Fernet
    os.environ["ADMETRICS_KEY"] = Fernet.generate_key().decode()

from core import store  # noqa: E402

R = random.Random(20261123)
TODAY = date.today()

MARKETERS = [
    ("m_anna", "М01 Анна", "anna@example.com", "Alpha"),
    ("m_boris", "М02 Борис", "boris@example.com", "Alpha"),
    ("m_vera", "М03 Вера", "vera@example.com", "Alpha"),
    ("m_grisha", "М04 Григорий", "grigory@example.com", "Beta"),
    ("m_dina", "М05 Дина", "dina@example.com", "Beta"),
    ("m_egor", "М06 Егор", "egor@example.com", "Beta"),
]
# Вес — насколько маркетолог «крупный»: от него зависит открут.
WEIGHT = {"m_anna": 1.6, "m_boris": 1.2, "m_vera": 0.9, "m_grisha": 1.4,
          "m_dina": 0.7, "m_egor": 0.5}

BMS = [
    ("100000000000001", "Northwind Media BM", "verified"),
    ("100000000000002", "Bluefin Ads BM", "verified"),
    ("100000000000003", "Orchid Digital BM", "not_verified"),
    ("100000000000004", "Pinecone Agency BM", "verified"),
]

GEOS = ["BR", "MX", "CL", "PE", "CO", "AR", "EC"]
OFFERS = ["Nutra", "Gambling", "Finance", "Dating", "Sweeps"]
REGIONS = {"BR": ["São Paulo", "Rio de Janeiro", "Minas Gerais", "Bahia"],
           "MX": ["Ciudad de México", "Jalisco", "Nuevo León"],
           "CL": ["Santiago Metropolitan"], "PE": ["Lima"],
           "CO": ["Bogotá", "Antioquia"], "AR": ["Buenos Aires"], "EC": ["Pichincha"]}
COLORS = ["#5b4fe0", "#0ea5a4", "#f97316", "#e11d48", "#16a34a", "#2563eb",
          "#a855f7", "#ca8a04"]


def d(day: date) -> str:
    return day.isoformat()


def money(x: float) -> str:
    return str(Decimal(x).quantize(Decimal("0.01")))


def thumb(text: str, color: str) -> str:
    """Превью креатива без внешних картинок: маленький SVG в data-URI."""
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='320' height='320'>"
           f"<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
           f"<stop offset='0' stop-color='{color}'/><stop offset='1' stop-color='#111827'/>"
           f"</linearGradient></defs><rect width='320' height='320' fill='url(#g)'/>"
           f"<circle cx='250' cy='70' r='46' fill='white' fill-opacity='.18'/>"
           f"<text x='24' y='270' font-family='Helvetica,Arial' font-size='34' "
           f"font-weight='700' fill='white'>{text}</text></svg>")
    from urllib.parse import quote
    return "data:image/svg+xml;utf8," + quote(svg)


def seed(days: int) -> None:
    db = Path(os.environ.get("ADMETRICS_DB", HERE / "admetrics.sqlite3"))
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    store.init_db()
    start = TODAY - timedelta(days=days - 1)
    all_days = [start + timedelta(days=i) for i in range(days)]

    # --- курсы ------------------------------------------------------------ #
    usd = 91.5
    for day in all_days:
        usd += R.uniform(-0.35, 0.35)
        store.set_rate(d(day), "USD", f"{usd:.4f}", source="demo")
        store.set_rate(d(day), "EUR", f"{usd * 1.08:.4f}", source="demo")
        store.set_rate(d(day), "RUB", "1", source="demo")

    # --- люди, прокси, лички ---------------------------------------------- #
    store.create_marketer("Админ", login="admin", role="admin",
                          marketer_id="m_admin", team="Alpha", tg_id="1000001")
    identities: dict[str, list[str]] = {}
    for n, (mid, name, email, team) in enumerate(MARKETERS):
        store.create_marketer(name, login=email, marketer_id=mid, team=team,
                              tg_id=str(1000010 + n))
        identities[mid] = []
        for k in range(2 if WEIGHT[mid] > 1 else 1):
            proxy = f"socks5://demo{n}{k}:demo-pass@203.0.113.{10 + n * 2 + k}:1080"
            store.add_proxy(proxy, label=f"BR-mobile-{n}{k}", marketer_id=mid)
            iid = store.add_identity(mid, json.dumps({"token": "EAADEMO", "demo": True}),
                                     proxy=proxy, label=f"Личка {name.split()[1]} #{k + 1}",
                                     session_name=f"S-{n + 1:02d}{k + 1}")
            identities[mid].append(iid)

    # --- BM ---------------------------------------------------------------- #
    with store.connect() as conn:
        for bm_id, name, ver in BMS:
            conn.execute(store._q("""
                INSERT INTO businesses (bm_id, name, verification_status,
                    is_disabled_for_integrity, credit_available, credit_currency,
                    created_time, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, 0, ?, 'USD', ?, ?, ?)"""),
                (bm_id, name, ver, money(R.uniform(200, 2500)),
                 d(TODAY - timedelta(days=R.randint(120, 600))), store.now(), store.now()))
            for u in range(R.randint(2, 4)):
                conn.execute(store._q("""
                    INSERT INTO business_users (bm_id, user_id, kind, name, email, role,
                        title, two_factor, expires_at, first_seen_at, last_seen_at)
                    VALUES (?, ?, 'user', ?, ?, ?, '', ?, NULL, ?, ?)"""),
                    (bm_id, f"{bm_id}{u}", f"Demo User {u + 1}",
                     f"user{u + 1}@example.com", "ADMIN" if u == 0 else "EMPLOYEE",
                     "two_fac_enabled" if u % 2 == 0 else "two_fac_not_enabled",
                     store.now(), store.now()))

    # --- кабинеты ---------------------------------------------------------- #
    accounts = []
    n_acc = 0
    for mid, name, email, team in MARKETERS:
        for _ in range(R.randint(2, 4)):
            n_acc += 1
            act = f"act_{9000000000 + n_acc * 7919}"
            geo = R.sample(GEOS, R.randint(1, 3))
            offer = R.choice(OFFERS)
            status = 1
            if n_acc in (5, 13):
                status = 2
            elif n_acc == 9:
                status = 3
            last4 = f"{R.randint(1000, 9999)}"
            created = TODAY - timedelta(days=R.randint(days + 5, days + 120))
            acc = {"act_id": act, "platform": "fb", "bm_id": R.choice(BMS)[0],
                   "name": f"{geo[0]} {offer} #{n_acc:02d}", "currency": "USD",
                   "timezone_name": "America/Sao_Paulo", "account_status": status,
                   "status_label": {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED"}[status],
                   "disable_reason": 1 if status == 2 else 0,
                   "disable_label": "ADS_INTEGRITY_POLICY" if status == 2 else "",
                   "amount_spent": "0", "balance": money(R.uniform(5, 180)),
                   "spend_cap": "0", "daily_spend_limit": str(R.choice([50, 250, 500, 1500])),
                   "funding_type": "card", "funding_last4": last4,
                   "funding_display": f"Visa *{last4}",
                   "created_time": d(created),
                   "next_bill_date": d(TODAY + timedelta(days=R.randint(1, 9)))}
            store.upsert_ad_account(acc)
            store.set_owner(act, mid, d(start - timedelta(days=30)))
            iid = R.choice(identities[mid])
            with store.connect() as conn:
                conn.execute(store._q("""
                    INSERT INTO ad_account_access (act_id, identity_id, role,
                        first_seen_at, last_seen_at) VALUES (?, ?, 'ADMIN', ?, ?)"""),
                    (act, iid, store.now(), store.now()))
            # Заблокированные «умерли» за 3–12 дней до сегодня.
            died = TODAY - timedelta(days=R.randint(3, 12)) if status == 2 else None
            accounts.append({**acc, "mid": mid, "geo": geo, "offer": offer,
                             "died": died, "last4": last4,
                             "scale": WEIGHT[mid] * R.uniform(0.6, 1.5),
                             "born": max(start, TODAY - timedelta(days=R.randint(20, days + 30)))})

    # --- открут ------------------------------------------------------------ #
    for a in accounts:
        act = a["act_id"]
        camps = []
        for c in range(R.randint(2, 4)):
            cid = f"{act[4:]}{c:02d}"
            geo = R.choice(a["geo"])
            objective = R.choice(["OUTCOME_LEADS", "OUTCOME_SALES", "OUTCOME_LEADS"])
            daily_budget = R.choice([120, 150, 200, 250, 300])
            status = "ACTIVE" if (a["account_status"] == 1 and R.random() > 0.2) else "PAUSED"
            camp = {"campaign_id": cid, "act_id": act,
                    "name": f"{geo} | {a['offer']} | {R.choice(['Broad', 'LAL 1%', 'Interests', 'Advantage+'])} | v{c + 1}",
                    "status": status, "effective_status": status,
                    "objective": objective, "buying_type": "AUCTION",
                    "daily_budget": str(daily_budget), "lifetime_budget": "0",
                    "budget_remaining": str(R.randint(0, daily_budget)),
                    "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "issues_json": "[]",
                    "created_time": d(a["born"])}
            store.upsert_campaign(camp)
            camp["geo"] = geo
            camp["share"] = R.uniform(0.5, 1.5)
            camp["cpl"] = R.uniform(2.2, 9.0)
            camps.append(camp)
            for s in range(R.randint(1, 3)):
                sid = f"{cid}{s}"
                store.upsert_adset({
                    "adset_id": sid, "act_id": act, "campaign_id": cid,
                    "name": f"{geo} {R.choice(['18-65', '25-54', '30-60'])} {R.choice(['all', 'android', 'ios'])}",
                    "effective_status": status, "status": status,
                    "optimization_goal": "LEAD_GENERATION" if "LEADS" in objective else "OFFSITE_CONVERSIONS",
                    "daily_budget": "0", "lifetime_budget": "0",
                    "age_min": R.choice([18, 21, 25]), "age_max": 65,
                    "genders": R.choice(["", "1", "2"]), "countries": geo,
                    "platforms": "facebook,instagram", "interests": R.choice(["", "Health, Fitness", "Investing, Personal finance"]),
                    "custom_audiences": "", "excluded": "", "devices": "mobile",
                    "locales": "", "billing_event": "IMPRESSIONS"})
                for k in range(R.randint(1, 3)):
                    ad_id = f"{sid}{k}"
                    title = R.choice(["Oferta só hoje", "Resultado em 7 dias",
                                      "Descubra o segredo", "Promoção limitada",
                                      "Aprovado por especialistas", "Ganhe bônus agora"])
                    store.upsert_ad({"ad_id": ad_id, "act_id": act, "campaign_id": cid,
                                     "adset_id": sid, "name": f"crea_{a['offer'].lower()}_{k + 1}.mp4",
                                     "effective_status": status, "issues_json": "[]",
                                     "story_id": ""})
                    store.upsert_ad_creative(ad_id, {
                        "title": title,
                        "body": "Texto do anúncio de demonstração. Todos os dados são fictícios.",
                        "thumb_url": thumb(f"{a['offer']} #{k + 1}", R.choice(COLORS)),
                        "preview_url": "", "link_url": f"https://offer-{a['offer'].lower()}.example/?sub={ad_id}",
                        "permalink": "", "instagram_url": "", "cta": "LEARN_MORE",
                        "object_type": "VIDEO", "creative_id": f"cr{ad_id}"})
                    camp.setdefault("ads", []).append(ad_id)

        daily_rows, country_rows, camp_rows, ad_rows, br_rows = [], [], [], [], []
        for day in all_days:
            if day < a["born"] or (a["died"] and day > a["died"]):
                continue
            age = (day - a["born"]).days
            ramp = min(1.0, 0.25 + age / 10)
            weekday = 0.85 if day.weekday() >= 5 else 1.0
            wave = 1 + 0.25 * __import__("math").sin(age / 4.5)
            base = 120 * a["scale"] * ramp * weekday * wave * R.uniform(0.75, 1.25)
            tot = {"spend": 0.0, "imp": 0, "clk": 0, "lc": 0, "leads": 0, "pur": 0, "rev": 0.0, "reach": 0}
            by_geo: dict[str, dict] = {}
            for c in camps:
                if c["status"] != "ACTIVE" and day > TODAY - timedelta(days=5):
                    continue
                sp = base * c["share"] / len(camps)
                cpm = R.uniform(1.8, 4.5)
                imp = int(sp / cpm * 1000)
                clk = int(imp * R.uniform(0.012, 0.028))
                lc = int(clk * R.uniform(0.7, 0.9))
                leads = int(sp / c["cpl"] * R.uniform(0.8, 1.2))
                pur = int(leads * 0.18) if "SALES" in c["objective"] else 0
                rev = pur * R.uniform(35, 60)
                reach = int(imp / R.uniform(1.3, 2.1))
                camp_rows.append({"date": d(day), "campaign_id": c["campaign_id"], "act_id": act,
                                  "campaign_name": c["name"], "objective": c["objective"],
                                  "spend": sp, "imp": imp, "reach": reach, "clk": clk, "lc": lc,
                                  "leads": leads, "pur": pur, "rev": rev})
                ads = c.get("ads", [])
                weights = [R.uniform(0.3, 1.5) for _ in ads]
                ws = sum(weights) or 1
                for ad_id, w in zip(ads, weights):
                    f = w / ws
                    ad_rows.append({"date": d(day), "ad_id": ad_id, "act_id": act,
                                    "campaign_id": c["campaign_id"], "currency": "USD",
                                    "spend": money(sp * f), "impressions": int(imp * f),
                                    "clicks": int(clk * f), "leads": int(leads * f),
                                    "purchases": int(pur * f), "link_clicks": int(lc * f),
                                    "revenue": money(rev * f)})
                g = by_geo.setdefault(c["geo"], {"spend": 0.0, "imp": 0, "clk": 0, "leads": 0, "pur": 0})
                g["spend"] += sp; g["imp"] += imp; g["clk"] += clk; g["leads"] += leads; g["pur"] += pur
                tot["spend"] += sp; tot["imp"] += imp; tot["clk"] += clk; tot["lc"] += lc
                tot["leads"] += leads; tot["pur"] += pur; tot["rev"] += rev; tot["reach"] += reach
            if tot["spend"] <= 0:
                continue
            daily_rows.append({"date": d(day), "act_id": act, "currency": "USD",
                               "spend": money(tot["spend"]), "impressions": tot["imp"],
                               "clicks": tot["clk"], "leads": tot["leads"],
                               "purchases": tot["pur"], "revenue": money(tot["rev"]),
                               "reach": tot["reach"], "link_clicks": tot["lc"]})
            for geo, g in by_geo.items():
                country_rows.append({"date": d(day), "act_id": act, "country": geo,
                                     "currency": "USD", "spend": money(g["spend"]),
                                     "impressions": g["imp"], "clicks": g["clk"],
                                     "leads": g["leads"], "purchases": g["pur"]})
            sp = tot["spend"]
            splits = {
                "age": {"18-24": .14, "25-34": .31, "35-44": .27, "45-54": .17, "55-64": .08, "65+": .03},
                "gender": {"female": .58, "male": .40, "unknown": .02},
                "platform": {"facebook": .63, "instagram": .29, "audience_network": .05, "messenger": .03},
                "device": {"android_smartphone": .71, "iphone": .24, "desktop": .05},
                "region": {r: 1 / sum(len(REGIONS[x]) for x in by_geo) for x in by_geo for r in REGIONS[x]},
            }
            for kind, parts in splits.items():
                for value, share in parts.items():
                    share *= R.uniform(0.85, 1.15)
                    br_rows.append({"date": d(day), "act_id": act, "kind": kind, "value": value,
                                    "currency": "USD", "spend": money(sp * share),
                                    "impressions": int(tot["imp"] * share),
                                    "clicks": int(tot["clk"] * share)})
        store.upsert_spend_daily(daily_rows)
        store.upsert_spend_country(country_rows)
        store.upsert_spend_ads(ad_rows)
        store.upsert_breakdowns(br_rows)
        with store.connect() as conn:
            for r in camp_rows:
                conn.execute(store._q("""
                    INSERT INTO spend_campaign_daily (date, platform, campaign_id, act_id,
                        campaign_name, objective, currency, spend, impressions, reach,
                        clicks, link_clicks, leads, purchases, revenue, updated_at)
                    VALUES (?, 'fb', ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
                    (r["date"], r["campaign_id"], r["act_id"], r["campaign_name"],
                     r["objective"], money(r["spend"]), r["imp"], r["reach"], r["clk"],
                     r["lc"], r["leads"], r["pur"], money(r["rev"]), store.now()))
        total = sum(float(r["spend"]) for r in daily_rows)
        store.upsert_ad_account({**{k: a[k] for k in a if k in (
            "act_id", "platform", "bm_id", "name", "currency", "timezone_name",
            "account_status", "status_label", "disable_reason", "disable_label",
            "balance", "spend_cap", "daily_spend_limit", "funding_type",
            "funding_last4", "funding_display", "created_time", "next_bill_date")},
            "amount_spent": money(total)})
        a["daily"] = daily_rows

        # --- списания FB и платежи с карты --------------------------------- #
        acc_since = 0.0
        threshold = R.choice([50, 100, 250, 500])
        for r in daily_rows:
            acc_since += float(r["spend"])
            if acc_since >= threshold:
                t = datetime.fromisoformat(r["date"]).replace(
                    hour=R.randint(0, 23), minute=R.randint(0, 59), tzinfo=timezone.utc)
                tx = f"tx{act[4:]}{r['date'].replace('-', '')}"
                declined = R.random() < 0.04
                store.upsert_fb_transaction({
                    "tx_id": tx, "act_id": act, "time": t.isoformat(),
                    "charge_type": "THRESHOLD", "status": "FAILED" if declined else "COMPLETED",
                    "payment_option": f"Visa *{a['last4']}", "currency": "USD",
                    "amount": money(acc_since), "tax_amount": "0"})
                store.upsert_card_payment({
                    "payment_id": f"p{tx}", "card_id": f"card_{a['last4']}",
                    "card_last4": a["last4"], "card_bin": "485932",
                    "card_title": f"FB {a['name']}",
                    "user_email": dict((m[0], m[2]) for m in MARKETERS)[a["mid"]],
                    "user_name": dict((m[0], m[1]) for m in MARKETERS)[a["mid"]],
                    "user_id": a["mid"], "team": dict((m[0], m[3]) for m in MARKETERS)[a["mid"]],
                    "marketer_id": a["mid"], "merchant": "Facebook",
                    "descriptor": "FACEBK *DEMO000000",
                    "state": "Declined" if declined else "Settled",
                    "decline": "insufficient-funds" if declined else "",
                    "is_micro": 0, "currency": "USD", "amount": money(acc_since),
                    "fee": money(acc_since * 0.01), "date": t.isoformat(),
                    "date_final": (t + timedelta(hours=R.randint(1, 30))).isoformat()})
                if not declined:
                    acc_since = 0.0
                    threshold = min(900, threshold * R.choice([1, 1, 2]))
        with store.connect() as conn:
            conn.execute(store._q("""
                INSERT INTO cards (card_id, last4, title, marketer_id, status, balance,
                    currency, updated_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?, 'USD', ?)
                ON CONFLICT (card_id) DO NOTHING"""),
                (f"card_{a['last4']}", a["last4"], f"FB {a['name']}", a["mid"],
                 money(R.uniform(40, 900)), store.now()))

        # --- снимки, пиксели, правила, история, люди в кабинете ------------ #
        with store.connect() as conn:
            if "SALES" in "".join(c["objective"] for c in camps) or R.random() > 0.4:
                conn.execute(store._q("""
                    INSERT INTO pixels (pixel_id, act_id, bm_id, name, last_fired_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)"""),
                    (f"px{act[4:]}", act, a["bm_id"], f"Pixel {a['offer']}",
                     (datetime.now(timezone.utc) - timedelta(minutes=R.randint(3, 300))).isoformat(),
                     store.now()))
            if R.random() > 0.5:
                conn.execute(store._q("""
                    INSERT INTO ad_rules (rule_id, act_id, name, status, action,
                        condition_text, schedule, created_time, updated_time, spec_json, updated_at)
                    VALUES (?, ?, ?, 'ENABLED', 'PAUSE', ?, 'каждые 30 мин', ?, ?, '{}', ?)"""),
                    (f"rule{act[4:]}", act, "Стоп по цене лида",
                     f"цена результата > {R.choice([6, 8, 10])} за сегодня",
                     d(a["born"]), d(a["born"]), store.now()))
            for i, (uname, tasks) in enumerate([("Demo Owner", "MANAGE,ADVERTISE,ANALYZE"),
                                                ("Demo Analyst", "ANALYZE")]):
                conn.execute(store._q("""
                    INSERT INTO account_users (act_id, user_id, name, tasks, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)"""), (act, f"u{i}{act[4:]}", uname, tasks,
                                                    store.now(), store.now()))
            events = [("update_campaign_budget", "Изменение бюджета кампании"),
                      ("update_ad_set_target_spec", "Изменение таргетинга"),
                      ("create_campaign_group", "Создана кампания"),
                      ("update_campaign_run_status", "Статус кампании изменён")]
            for e in range(R.randint(3, 7)):
                et, label = R.choice(events)
                c = R.choice(camps)
                when = datetime.combine(TODAY - timedelta(days=R.randint(0, 20)),
                                        datetime.min.time(), timezone.utc) + timedelta(hours=R.randint(7, 22))
                conn.execute(store._q("""
                    INSERT INTO account_activities (id, act_id, event_time, event_type,
                        event_label, actor_id, actor_name, object_id, object_name,
                        extra_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
                    (f"ev{act[4:]}{e}", act, when.isoformat(), et, label, "u0",
                     "Demo Owner", c["campaign_id"], c["name"],
                     json.dumps({"old_value": "50.00", "new_value": "80.00"}), store.now()))
        for back in (1, 0):
            day = TODAY - timedelta(days=back)
            store.save_snapshot(act, d(day), {
                "account_status": a["account_status"] if back == 0 else 1,
                "disable_reason": a["disable_reason"], "daily_spend_limit": a["daily_spend_limit"],
                "spend_cap": "0", "balance": a["balance"], "amount_spent": "0",
                "funding_last4": a["last4"]})

    # --- статусы личек ---------------------------------------------------- #
    flat = [(mid, iid) for mid, ids in identities.items() for iid in ids]
    for n, (mid, iid) in enumerate(flat):
        if n == 3:
            store.set_identity_status(iid, "needs_reconnect",
                                      "Токен отозван: сменился пароль аккаунта")
        else:
            store.set_identity_status(iid, "ok", "")

    # --- алерты ------------------------------------------------------------- #
    from core import alerts
    for a in accounts:
        if a["account_status"] == 2:
            alerts.raise_alert("account_dead", f"Кабинет {a['name']} заблокирован",
                               "Причина: ADS_INTEGRITY_POLICY", severity="bad",
                               act_id=a["act_id"], marketer_id=a["mid"])
    a = accounts[1]
    alerts.raise_alert("limit_changed", f"{a['name']}: дневной лимит 250 → 500",
                       "Meta подняла лимит", severity="info", act_id=a["act_id"], marketer_id=a["mid"])
    a = accounts[3]
    alerts.raise_alert("card_changed", f"{a['name']}: сменилась карта",
                       f"было •1234, стало •{a['last4']}", severity="warn",
                       act_id=a["act_id"], marketer_id=a["mid"])
    alerts.raise_alert("identity_dead", "Личка требует переподключения",
                       "Токен отозван: сменился пароль аккаунта", severity="bad",
                       identity_id=flat[3][1], marketer_id=flat[3][0])

    # --- журнал действий из панели ----------------------------------------- #
    with store.connect() as conn:
        for k in range(8):
            a = R.choice(accounts)
            when = datetime.now(timezone.utc) - timedelta(hours=R.randint(1, 200))
            op, before, after = R.choice([("budget", "50.00", "80.00"), ("pause", "ACTIVE", "PAUSED"),
                                          ("resume", "PAUSED", "ACTIVE"), ("budget", "120.00", "90.00")])
            conn.execute(store._q("""
                INSERT INTO control_actions (at, actor_id, actor_name, actor_role, act_id,
                    target_kind, target_id, target_name, op, before_val, after_val, status, error)
                VALUES (?, ?, ?, 'marketer', ?, 'campaign', ?, ?, ?, ?, ?, 'ok', '')"""),
                (when.isoformat(), a["mid"], dict((m[0], m[1]) for m in MARKETERS)[a["mid"]],
                 a["act_id"], f"{a['act_id'][4:]}00", f"{a['geo'][0]} | {a['offer']}", op, before, after))

        # --- история сборов (раздел «Состояние») -------------------------- #
        for k in range(8):
            st = datetime.now(timezone.utc) - timedelta(minutes=10 * k + 3)
            conn.execute(store._q("""
                INSERT INTO collection_runs (started_at, finished_at, kind, status, stats_json)
                VALUES (?, ?, 'cycle', 'done', ?)"""),
                (st.isoformat(), (st + timedelta(seconds=R.randint(40, 95))).isoformat(),
                 json.dumps({"identities": len(flat), "accounts": len(accounts),
                             "errors": 1 if k == 2 else 0})))

        # --- страницы для чистки комментариев ------------------------------ #
        for n, (mid, name, *_rest) in enumerate(MARKETERS[:4]):
            conn.execute(store._q("""
                INSERT INTO page_tokens (page_id, name, token_encrypted, identity_id,
                    marketer_id, can_moderate, auto_clean, last_cleaned_at, last_deleted, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)"""),
                (f"pg{n}", f"Saúde Natural {n + 1}", store.encrypt("demo"), identities[mid][0],
                 mid, 1 if n % 2 == 0 else 0,
                 (datetime.now(timezone.utc) - timedelta(minutes=R.randint(5, 90))).isoformat(),
                 R.randint(0, 40), store.now()))

        # --- заказы SMM ------------------------------------------------------ #
        for k in range(5):
            when = datetime.now(timezone.utc) - timedelta(hours=R.randint(2, 120))
            conn.execute(store._q("""
                INSERT INTO smm_orders (order_id, service_id, service_name, category, link,
                    quantity, charge, currency, start_count, remains, status, marketer_id,
                    created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?)"""),
                (f"{700100 + k}", "101", R.choice(["Лайки на пост", "Подписчики страницы", "Отзывы"]),
                 "Facebook", f"https://facebook.example/post/{k}", R.choice([100, 250, 500]),
                 money(R.uniform(1, 9)), R.randint(10, 300), 0 if k else 120,
                 "Completed" if k else "In progress", R.choice(MARKETERS)[0],
                 when.isoformat(), when.isoformat()))

    store.kv_set("demo", "1")
    print(f"Готово: {len(MARKETERS)} маркетологов, {len(accounts)} кабинетов, "
          f"{days} дней истории → {db}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--days", type=int, default=50)
    seed(ap.parse_args().days)
