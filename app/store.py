import time, json, os
from urllib.parse import quote
from .db import db
import datetime as dt, secrets
from .config import DATA_DIR, FREE_DELAY, SETTING_DEFAULTS

def now(): return int(time.time())

def add_deal(kind, title, url, summary="", merchant="", origin="", dest="", price=None,
             code="", source="manual", uid=None, silent=0, expires_at=None):
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO deals(kind,title,summary,merchant,url,origin,dest,price,code,source,uid,silent,created_at,expires_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, title, summary, merchant, url, origin, dest, price, code, source, uid, silent, now(), expires_at))
            return cur.lastrowid
        except Exception:   # uid 重複
            return None

def load_json(name, default):
    path = os.path.join(DATA_DIR, name)
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default

def wrap_affiliate(url, merchant):
    """data/affiliate.json: {"klook": "https://...{url}...", "trip.com": "..."} ; {url} 會被 URL-encode 後代入"""
    tpl = load_json("affiliate.json", {}).get((merchant or "").lower())
    if tpl and "{url}" in tpl:
        return tpl.replace("{url}", quote(url, safe=""))
    return url

def is_vip(row):
    return bool(row and row["vip_until"] and row["vip_until"] > now())

_cache = {}
def get_setting(k):
    hit = _cache.get(k)
    if hit and time.time() - hit[0] < 5: return hit[1]
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    v = r["value"] if r else SETTING_DEFAULTS.get(k, "")
    _cache[k] = (time.time(), v); return v

def set_setting(k, v):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    _cache.pop(k, None)

def on(k): return get_setting(k) == "1"

def free_delay():
    try: return max(int(float(get_setting("free_delay_min"))), 0) * 60
    except ValueError: return FREE_DELAY

def locked(deal_created_at):
    return now() - deal_created_at < free_delay()

HKT = dt.timezone(dt.timedelta(hours=8))
def hk_today(): return dt.datetime.now(HKT).strftime("%Y-%m-%d")

def bump(key, n=1):
    with db() as c:
        c.execute("INSERT INTO daily_stats(day,key,n) VALUES(?,?,?) ON CONFLICT(day,key) DO UPDATE SET n=n+excluded.n", (hk_today(), key, n))

def grant_vip(days, tg=None, email=None):
    """為 Telegram chat_id 及/或 Email 延長 VIP;回傳 (需要發確認信的 email 或 None)"""
    t, need_confirm = now(), None
    with db() as c:
        if tg:
            c.execute("INSERT OR IGNORE INTO subscribers(chat_id,created_at) VALUES(?,?)", (tg, t))
            cur = c.execute("SELECT vip_until FROM subscribers WHERE chat_id=?", (tg,)).fetchone()["vip_until"] or 0
            c.execute("UPDATE subscribers SET vip_until=? WHERE chat_id=?", (max(cur, t) + days * 86400, tg))
        if email:
            email = email.strip().lower()
            c.execute("INSERT OR IGNORE INTO email_subs(email,token,created_at) VALUES(?,?,?)", (email, secrets.token_urlsafe(24), t))
            row = c.execute("SELECT * FROM email_subs WHERE email=?", (email,)).fetchone()
            c.execute("UPDATE email_subs SET vip_until=? WHERE email=?", (max(row["vip_until"] or 0, t) + days * 86400, email))
            if not row["confirmed"]: need_confirm = email
    return need_confirm


# ---------- 密鑰:後台(DB)優先,否則環境變數 ----------
SECRET_ENV = {"resend_api_key": "RESEND_API_KEY", "mail_from": "MAIL_FROM", "tp_token": "TP_TOKEN", "tp_marker": "TP_MARKER",
              "ignav_key": "IGNAV_API_KEY", "serpapi_key": "SERPAPI_KEY", "anthropic_key": "ANTHROPIC_API_KEY", "stripe_secret": "STRIPE_SECRET_KEY", "stripe_webhook_secret": "STRIPE_WEBHOOK_SECRET"}

def get_secret(name):
    v = get_setting("secret:" + name)
    return v or os.getenv(SECRET_ENV.get(name, ""), "") or ("著數飛 <deals@example.com>" if name == "mail_from" else "")

def secret_source(name):
    if get_setting("secret:" + name): return "後台"
    return "環境變數" if os.getenv(SECRET_ENV.get(name, ""), "") else "未設定"

def mask(v): return ("••••" + v[-4:]) if v and len(v) > 8 else ("••••" if v else "")

# ---------- 航線 / 來源預設值 ----------
def get_routes():
    with db() as c:
        return [(r["origin"], r["dest"]) for r in c.execute("SELECT origin,dest FROM routes WHERE enabled=1 ORDER BY id")]

def seed_defaults():
    from .config import ROUTES, POLL_SECONDS, MONTHS_AHEAD
    with db() as c:
        if not c.execute("SELECT 1 FROM routes LIMIT 1").fetchone():
            for o, d in ROUTES: c.execute("INSERT OR IGNORE INTO routes(origin,dest) VALUES(?,?)", (o, d))
        c.execute("INSERT OR IGNORE INTO providers(key,enabled,interval_min,config) VALUES('travelpayouts',?,?,?)",
                  (1 if get_secret("tp_token") else 0, max(POLL_SECONDS // 60, 5), json.dumps({"months_ahead": MONTHS_AHEAD})))
        c.execute("INSERT OR IGNORE INTO providers(key,enabled,interval_min,config) VALUES('ignav',?,?,?)",
                  (1 if get_secret("ignav_key") else 0, 360, json.dumps({"offsets": [21, 45, 75, 105], "trip_days": 5, "market": "HK"})))
        c.execute("INSERT OR IGNORE INTO providers(key,enabled,interval_min,config) VALUES('serpapi',?,?,?)",
                  (1 if get_secret("serpapi_key") else 0, 1440, json.dumps({"offsets": [45], "trip_days": 5})))
        if not c.execute("SELECT 1 FROM custom_sources LIMIT 1").fetchone():
            old = load_json("sources.json", {})
            for fd in old.get("feeds", []):
                c.execute("INSERT OR IGNORE INTO custom_sources(kind,name,config) VALUES('feed',?,?)", (fd.get("name", "feed"), json.dumps(fd)))
            for sc in old.get("scrapers", []):
                c.execute("INSERT OR IGNORE INTO custom_sources(kind,name,config) VALUES('scrape',?,?)", (sc.get("name", "scrape"), json.dumps(sc)))
