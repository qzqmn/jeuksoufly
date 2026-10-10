import datetime as dt
from .db import db
from .store import now, HKT

def _series(c, sql, days):
    rows = {r["d"]: r["n"] for r in c.execute(sql, (now() - days * 86400,))}
    today = dt.datetime.now(HKT).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d"); out.append((d[5:], rows.get(d, 0)))
    return out

def dashboard():
    t = now()
    day0 = int(dt.datetime.now(HKT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with db() as c:
        one = lambda q, *p: c.execute(q, p).fetchone()[0] or 0
        pv = lambda days: one("SELECT SUM(n) FROM daily_stats WHERE key='pv' AND day>=date('now','+8 hours',?)", f"-{days - 1} days")
        d = dict(
            pv_today=pv(1), pv_7=pv(7), pv_30=pv(30),
            clicks_today=one("SELECT COUNT(*) FROM clicks WHERE ts>=?", day0),
            clicks_7=one("SELECT COUNT(*) FROM clicks WHERE ts>?", t - 7 * 86400),
            clicks_30=one("SELECT COUNT(*) FROM clicks WHERE ts>?", t - 30 * 86400),
            tg_total=one("SELECT COUNT(*) FROM subscribers"), tg_new7=one("SELECT COUNT(*) FROM subscribers WHERE created_at>?", t - 7 * 86400),
            tg_vip=one("SELECT COUNT(*) FROM subscribers WHERE vip_until>?", t),
            em_total=one("SELECT COUNT(*) FROM email_subs"), em_ok=one("SELECT COUNT(*) FROM email_subs WHERE confirmed=1"),
            em_new7=one("SELECT COUNT(*) FROM email_subs WHERE created_at>?", t - 7 * 86400),
            em_vip=one("SELECT COUNT(*) FROM email_subs WHERE vip_until>?", t),
            pay30=one("SELECT SUM(amount) FROM payments WHERE status='paid' AND ts>?", t - 30 * 86400),
            pay_pending=one("SELECT COUNT(*) FROM payments WHERE status='pending'"),
            aff30=one("SELECT SUM(amount) FROM revenue WHERE day>=date('now','+8 hours','-29 days')"),
            pushes24=one("SELECT COUNT(*) FROM sent WHERE ts>?", t - 86400), emails24=one("SELECT COUNT(*) FROM email_sent WHERE ts>?", t - 86400),
            price_pts24=one("SELECT COUNT(*) FROM price_history WHERE ts>?", t - 86400),
            price_last=one("SELECT MAX(ts) FROM price_history"),
            cards_changed=one("SELECT COUNT(*) FROM card_watch WHERE reviewed=0"),
            inbox_new=one("SELECT COUNT(*) FROM inbox WHERE status='new'"),
            ai_calls7=one("SELECT SUM(n) FROM daily_stats WHERE key='ai_calls' AND day>=date('now','+8 hours','-6 days')"),
            ai_deals7=one("SELECT SUM(n) FROM daily_stats WHERE key='ai_deals' AND day>=date('now','+8 hours','-6 days')"),
            deals24=one("SELECT COUNT(*) FROM deals WHERE created_at>?", t - 86400),
        )
        d["ctr_7"] = round(d["clicks_7"] / d["pv_7"] * 100, 1) if d["pv_7"] else 0
        d["click_series"] = _series(c, "SELECT date(ts,'unixepoch','+8 hours') d, COUNT(*) n FROM clicks WHERE ts>? GROUP BY d", 14)
        pv_rows = {r["day"]: r["n"] for r in c.execute("SELECT day,n FROM daily_stats WHERE key='pv'")}
        td = dt.datetime.now(HKT).date()
        d["pv_series"] = [((td - dt.timedelta(days=i)).strftime("%m-%d"), pv_rows.get((td - dt.timedelta(days=i)).strftime("%Y-%m-%d"), 0)) for i in range(13, -1, -1)]
        d["top_deals"] = c.execute("""SELECT d.id,d.title,d.merchant,COUNT(*) n FROM clicks k JOIN deals d ON k.target='deal:'||d.id
                                       WHERE k.ts>? GROUP BY d.id ORDER BY n DESC LIMIT 8""", (t - 7 * 86400,)).fetchall()
        d["by_merchant"] = c.execute("""SELECT COALESCE(NULLIF(d.merchant,''),'(未標記)') m, COUNT(*) n FROM clicks k JOIN deals d ON k.target='deal:'||d.id
                                         WHERE k.ts>? GROUP BY m ORDER BY n DESC LIMIT 8""", (t - 7 * 86400,)).fetchall()
        d["card_clicks"] = c.execute("SELECT target, COUNT(*) n FROM clicks WHERE target LIKE 'card:%' AND ts>? GROUP BY target ORDER BY n DESC", (t - 7 * 86400,)).fetchall()
        d["rev_by_src"] = c.execute("SELECT source, SUM(amount) n FROM revenue WHERE day>=date('now','+8 hours','-29 days') GROUP BY source ORDER BY n DESC").fetchall()
        d["sources"] = c.execute("SELECT * FROM source_status ORDER BY name").fetchall()
    return d
