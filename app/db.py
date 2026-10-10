import sqlite3, os
from contextlib import contextmanager
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'promo',       -- flight | promo | cn | card
  title TEXT NOT NULL, summary TEXT DEFAULT '', merchant TEXT DEFAULT '',
  url TEXT NOT NULL, origin TEXT DEFAULT '', dest TEXT DEFAULT '',
  price REAL, currency TEXT DEFAULT 'HKD', code TEXT DEFAULT '',
  source TEXT DEFAULT 'manual', uid TEXT UNIQUE,
  silent INTEGER DEFAULT 0, created_at INTEGER NOT NULL, expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_deals_created ON deals(created_at DESC);
CREATE TABLE IF NOT EXISTS price_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  origin TEXT, dest TEXT, depart_date TEXT, price REAL, airline TEXT DEFAULT '', link TEXT DEFAULT '', ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ph_route ON price_history(origin, dest, ts);
CREATE TABLE IF NOT EXISTS subscribers(
  chat_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', vip_until INTEGER DEFAULT 0,
  dests TEXT DEFAULT '', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, origin TEXT, dest TEXT, max_price REAL,
  last_price REAL, last_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sent(deal_id INTEGER, chat_id INTEGER, ts INTEGER, PRIMARY KEY(deal_id, chat_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS email_subs(
  email TEXT PRIMARY KEY, token TEXT UNIQUE, confirmed INTEGER DEFAULT 0, vip_until INTEGER DEFAULT 0,
  created_at INTEGER, last_digest TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS email_sent(deal_id INTEGER, email TEXT, ts INTEGER, PRIMARY KEY(deal_id, email));
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, ref TEXT UNIQUE, tg INTEGER, email TEXT DEFAULT '',
  amount REAL, days INTEGER, status TEXT, note TEXT DEFAULT '', ts INTEGER
);
CREATE TABLE IF NOT EXISTS revenue(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, source TEXT, amount REAL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS daily_stats(day TEXT, key TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS source_status(
  name TEXT PRIMARY KEY, last_run INTEGER, last_ok INTEGER, last_error TEXT DEFAULT '', last_count INTEGER DEFAULT 0,
  runs INTEGER DEFAULT 0, fails INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT, dest TEXT, enabled INTEGER DEFAULT 1, UNIQUE(origin,dest));
CREATE TABLE IF NOT EXISTS providers(key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0, interval_min INTEGER DEFAULT 360, config TEXT DEFAULT '{}', last_run INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS custom_sources(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT UNIQUE, enabled INTEGER DEFAULT 1, config TEXT DEFAULT '{}', last_run INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS card_watch(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, last_hash TEXT DEFAULT '', last_checked INTEGER DEFAULT 0, changed_at INTEGER DEFAULT 0, reviewed INTEGER DEFAULT 1, error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, uid TEXT UNIQUE, title TEXT, url TEXT, summary TEXT DEFAULT '', kind TEXT DEFAULT 'promo', merchant TEXT DEFAULT '', status TEXT DEFAULT 'new', ts INTEGER);
CREATE TABLE IF NOT EXISTS stripe_customers(customer_id TEXT, email TEXT DEFAULT '', tg INTEGER, PRIMARY KEY(customer_id, email));
CREATE TABLE IF NOT EXISTS magic_tokens(token TEXT PRIMARY KEY, customer_id TEXT, expires INTEGER);
CREATE TABLE IF NOT EXISTS promo_watch(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, merchant TEXT DEFAULT '', mode TEXT DEFAULT 'review',
  interval_hours INTEGER DEFAULT 6, enabled INTEGER DEFAULT 1, last_hash TEXT DEFAULT '', last_checked INTEGER DEFAULT 0,
  last_changed INTEGER DEFAULT 0, last_status TEXT DEFAULT '', last_count INTEGER DEFAULT 0, first_done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS clicks(id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT, ts INTEGER, ref TEXT DEFAULT '');
"""

@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(price_history)")]
        icols = [r[1] for r in c.execute("PRAGMA table_info(inbox)")]
        for col, ddl in (("dest", "TEXT DEFAULT ''"), ("price", "REAL"), ("expires_at", "INTEGER")):
            if col not in icols: c.execute(f"ALTER TABLE inbox ADD COLUMN {col} {ddl}")
        if "provider" not in cols:
            c.execute("ALTER TABLE price_history ADD COLUMN provider TEXT DEFAULT 'travelpayouts'")
