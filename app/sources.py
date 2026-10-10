import re, time, json, logging, hashlib, statistics, datetime as dt
from urllib.parse import quote
import xml.etree.ElementTree as ET
import httpx
from bs4 import BeautifulSoup
from . import config as C
from .db import db
from .store import add_deal, load_json, now, get_secret, get_routes, get_setting

log = logging.getLogger("sources")
UA = "Mozilla/5.0 (compatible; JeukSouFlyBot/1.0)"

def mark(name, ok, err="", count=0):
    t = now()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO source_status(name) VALUES(?)", (name,))
        c.execute("UPDATE source_status SET last_run=?, runs=runs+1, fails=fails+?, last_error=?, last_count=?, last_ok=CASE WHEN ? THEN ? ELSE last_ok END WHERE name=?",
                  (t, 0 if ok else 1, err[:200], count, 1 if ok else 0, t, name))

def _months(n):
    d = dt.date.today().replace(day=1)
    for _ in range(n):
        yield d.strftime("%Y-%m")
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)

def guess_dest(text):
    for code, name in C.CITY.items():
        if code != "HKG" and name in text: return code
    return ""

# ================= 票價記錄與降價偵測(各來源分開比較) =================
def record_price(origin, dest, depart, price, airline, link, provider):
    ts = now()
    min_hist = int(float(get_setting("min_history") or 8)); ratio = float(get_setting("deal_drop_ratio") or C.DEAL_DROP_RATIO)
    with db() as c:
        hist = [r["price"] for r in c.execute(
            "SELECT price FROM price_history WHERE origin=? AND dest=? AND provider=? AND ts>? AND ts<?",
            (origin, dest, provider, ts - 30 * 86400, ts - 3600))]
        c.execute("INSERT INTO price_history(origin,dest,depart_date,price,airline,link,ts,provider) VALUES(?,?,?,?,?,?,?,?)",
                  (origin, dest, depart, price, airline, link, ts, provider))
    if len(hist) >= min_hist:
        med = statistics.median(hist)
        if price <= med * ratio:
            return dict(origin=origin, dest=dest, depart=depart, price=price, airline=airline, med=med, provider=provider)
    return None

def publish_flight_deal(dl, link, merchant):
    o, d = dl["origin"], dl["dest"]
    pct = round((1 - dl["price"] / dl["med"]) * 100)
    title = f"{C.CITY.get(o, o)}→{C.CITY.get(d, d)} 來回低至 HK${dl['price']:,.0f}({dl['depart']} 出發)"
    add_deal("flight", title, link, summary=f"較近 30 日中位數 HK${dl['med']:,.0f} 平 {pct}%" + (f",{dl['airline']}" if dl["airline"] else ""),
             merchant=merchant, origin=o, dest=d, price=dl["price"], source=dl["provider"],
             uid=f"fl:{dl['provider']}:{o}-{d}:{dl['depart']}:{int(dl['price'])}")

def _gf_link(o, d, date):
    return "https://www.google.com/travel/flights?q=" + quote(f"Flights from {o} to {d} on {date}")

# ================= 來源 1:Travelpayouts(快取價) =================
async def tp_fetch(client, routes, cfg, publish=True):
    token, marker = get_secret("tp_token"), get_secret("tp_marker")
    if not token: return [dict(route="—", points=0, best=None, ok=False, err="未設定 Travelpayouts token")]
    rows = []
    for o, d in routes:
        got, best, err = 0, None, ""
        for month in _months(int(cfg.get("months_ahead", C.MONTHS_AHEAD))):
            try:
                r = await client.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates", params={
                    "origin": o, "destination": d, "departure_at": month, "one_way": "false", "currency": "hkd",
                    "sorting": "price", "limit": 1, "token": token})
                r.raise_for_status(); data = r.json().get("data") or []
            except Exception as e:
                err = str(e)[:150]; continue
            if not data: continue
            it = data[0]; price = float(it["price"]); got += 1
            best = price if best is None else min(best, price)
            link = "https://www.aviasales.com" + it.get("link", "")
            if marker: link += ("&" if "?" in link else "?") + "marker=" + marker
            deal = record_price(o, d, (it.get("departure_at") or month)[:10], price, it.get("airline", ""), link, "travelpayouts")
            if deal and publish: publish_flight_deal(deal, link, "aviasales")
        ok = got > 0
        rows.append(dict(route=f"{o}-{d}", points=got, best=best, ok=ok, err=err or ("API 無數據(快取未覆蓋)" if not ok else "")))
        mark(f"travelpayouts:{o}-{d}", ok, rows[-1]["err"], got)
    return rows

# ================= 來源 2:Ignav(即時搜尋價,按請求收費) =================
async def _ignav_link(client, key, ignav_id, fallback):
    try:
        r = await client.post("https://ignav.com/api/fares/booking-links", headers={"X-Api-Key": key}, json={"ignav_id": ignav_id})
        r.raise_for_status()
        links = [l for opt in r.json().get("booking_options", []) for l in opt.get("links", []) if l.get("url")]
        if not links: return fallback, ""
        aff = {k.lower() for k in load_json("affiliate.json", {})}
        pick = next((l for l in links if any(k in l.get("provider_name", "").lower() for k in aff)), None) \
            or next((l for l in links if l.get("provider_type") == "airline"), links[0])
        url = pick["url"]
        if not url.startswith("http"): url = "https://" + url
        return url, pick.get("provider_name", "").lower()
    except Exception as e:
        log.warning("ignav booking-links failed: %s", e); return fallback, ""

async def ignav_fetch(client, routes, cfg, publish=True):
    key = get_secret("ignav_key")
    if not key: return [dict(route="—", points=0, best=None, ok=False, err="未設定 Ignav API key")]
    market, trip = cfg.get("market", "HK"), int(cfg.get("trip_days", 5))
    rows = []
    for o, d in routes:
        got, best, err = 0, None, ""
        for off in cfg.get("offsets", [21, 45, 75, 105]):
            dep = dt.date.today() + dt.timedelta(days=int(off)); ret = dep + dt.timedelta(days=trip)
            try:
                r = await client.post("https://ignav.com/api/fares/round-trip", headers={"X-Api-Key": key}, json={
                    "origin": o, "destination": d, "departure_date": dep.isoformat(), "return_date": ret.isoformat(), "market": market})
                r.raise_for_status(); its = r.json().get("itineraries") or []
            except Exception as e:
                err = str(e)[:150]; continue
            ok_its = [i for i in its if (i.get("price") or {}).get("amount", 0) > 0 and i["price"].get("currency") == "HKD"]
            if not ok_its:
                if its: err = f"幣別非 HKD({its[0].get('price', {}).get('currency')}),已略過"
                continue
            it = min(ok_its, key=lambda x: x["price"]["amount"])
            price = float(it["price"]["amount"]); airline = (it.get("outbound") or {}).get("carrier", "")
            got += 1; best = price if best is None else min(best, price)
            fb = _gf_link(o, d, dep.isoformat())
            deal = record_price(o, d, dep.isoformat(), price, airline, fb, "ignav")
            if deal and publish:
                link, merchant = await _ignav_link(client, key, it.get("ignav_id"), fb)
                publish_flight_deal(deal, link, merchant)
        ok = got > 0
        rows.append(dict(route=f"{o}-{d}", points=got, best=best, ok=ok, err=err or ("無結果" if not ok else "")))
        mark(f"ignav:{o}-{d}", ok, rows[-1]["err"], got)
    return rows

# ================= 來源 3:SerpApi Google Flights(含 Google 的「價格偏低」判斷) =================
SERP_AIRPORTS = {"TYO": "NRT,HND", "OSA": "KIX,ITM", "SEL": "ICN,GMP", "BKK": "BKK,DMK", "NGO": "NGO", "TPE": "TPE,TSA"}

def _aviasales_search(o, d, dep, ret, marker):
    f = lambda x: x.strftime("%d%m")
    return f"https://www.aviasales.com/search/{o}{f(dep)}{d}{f(ret)}" + (f"?marker={marker}" if marker else "")

async def serpapi_fetch(client, routes, cfg, publish=True):
    key = get_secret("serpapi_key")
    if not key: return [dict(route="—", points=0, best=None, ok=False, err="未設定 SerpApi API key")]
    amap = {**SERP_AIRPORTS, **cfg.get("airport_map", {})}; trip = int(cfg.get("trip_days", 5)); marker = get_secret("tp_marker")
    rows = []
    for o, d in routes:
        got, best_price, err = 0, None, ""
        for off in cfg.get("offsets", [45]):
            dep = dt.date.today() + dt.timedelta(days=int(off)); ret = dep + dt.timedelta(days=trip)
            try:
                r = await client.get("https://serpapi.com/search.json", params={
                    "engine": "google_flights", "departure_id": amap.get(o, o), "arrival_id": amap.get(d, d),
                    "outbound_date": dep.isoformat(), "return_date": ret.isoformat(), "type": "1",
                    "currency": "HKD", "hl": "zh-TW", "gl": "hk", "api_key": key})
                r.raise_for_status(); j = r.json()
                if j.get("error"): raise RuntimeError(j["error"])
            except Exception as e:
                err = str(e)[:150]; continue
            fl = [f for f in (j.get("best_flights") or []) + (j.get("other_flights") or []) if isinstance(f.get("price"), (int, float)) and f["price"] > 0]
            if not fl: continue
            top = min(fl, key=lambda f: f["price"]); price = float(top["price"])
            airline = ((top.get("flights") or [{}])[0]).get("airline", "")
            got += 1; best_price = price if best_price is None else min(best_price, price)
            gf = (j.get("search_metadata") or {}).get("google_flights_url") or _gf_link(o, d, dep.isoformat())
            link, merchant = (_aviasales_search(o, d, dep, ret, marker), "aviasales") if (marker and cfg.get("link_via_aviasales", True)) else (gf, "")
            ins = j.get("price_insights") or {}; rng = ins.get("typical_price_range") or []
            deal = record_price(o, d, dep.isoformat(), price, airline, gf, "serpapi")
            if publish and ins.get("price_level") == "low":
                note = f"Google Flights 標示此日期價格偏低" + (f"(典型 HK${rng[0]:,.0f}–{rng[1]:,.0f})" if len(rng) == 2 else "")
                add_deal("flight", f"{C.CITY.get(o, o)}→{C.CITY.get(d, d)} 來回低至 HK${price:,.0f}({dep.isoformat()} 出發)", link,
                         summary=note + (f",{airline}" if airline else ""), merchant=merchant, origin=o, dest=d, price=price,
                         source="serpapi", uid=f"fl:serpapi:{o}-{d}:{dep.isoformat()}:{int(price)}")
            elif publish and deal:
                publish_flight_deal(deal, link, merchant)
        ok = got > 0
        rows.append(dict(route=f"{o}-{d}", points=got, best=best_price, ok=ok, err=err or ("無結果" if not ok else "")))
        mark(f"serpapi:{o}-{d}", ok, rows[-1]["err"], got)
    return rows

PROVIDERS = {"travelpayouts": tp_fetch, "ignav": ignav_fetch, "serpapi": serpapi_fetch}

# ================= RSS / Atom / CSS 爬蟲(來源由後台管理) =================
def _strip(tag): return tag.rsplit("}", 1)[-1]

def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    for node in root.iter():
        if _strip(node.tag) in ("item", "entry"):
            f = {_strip(ch.tag): ch for ch in node}
            title = (f["title"].text or "").strip() if "title" in f else ""
            link = (f["link"].get("href") or (f["link"].text or "")) if "link" in f else ""
            desc = ""
            for k in ("description", "summary", "content"):
                if k in f and f[k].text:
                    desc = BeautifulSoup(f[k].text, "html.parser").get_text(" ", strip=True)[:200]; break
            if title and link: yield title, link.strip(), desc

def _kws(): return [k.strip() for k in (get_setting("keywords") or "").split(",") if k.strip()]

def _ingest(source_name, items, cfg):
    kws = _kws()
    if not cfg.get("auto"):      # 預設:先進「收件匣」人手審核,避免直接轉載他人內容
        n = 0
        for title, link, desc in items:
            if kws and not any(k.lower() in (title + desc).lower() for k in kws): continue
            with db() as c:
                cur = c.execute("INSERT OR IGNORE INTO inbox(source,uid,title,url,summary,kind,merchant,ts) VALUES(?,?,?,?,?,?,?,?)",
                                (source_name, f"{source_name}:{link}", title, link, desc, cfg.get("kind", "promo"), cfg.get("merchant", ""), now()))
                n += cur.rowcount
        return n
    with db() as c:
        first = c.execute("SELECT COUNT(*) n FROM deals WHERE source=?", (source_name,)).fetchone()["n"] == 0
    n = 0
    for title, link, desc in items:
        if kws and not any(k.lower() in (title + desc).lower() for k in kws): continue
        if first and n >= 5: break
        did = add_deal(cfg.get("kind", "promo"), title, link, summary=desc, merchant=cfg.get("merchant", ""),
                       dest=guess_dest(title), source=source_name, uid=f"{source_name}:{link}", silent=1 if first else 0)
        if did: n += 1
    return n

async def run_custom(client, src, publish=True):
    cfg = json.loads(src["config"] or "{}"); name = f"{src['kind']}:{src['name']}"
    try:
        r = await client.get(cfg["url"]); r.raise_for_status()
        if src["kind"] == "feed":
            items = list(parse_feed(r.text))
        else:
            soup = BeautifulSoup(r.text, "html.parser"); items = []
            for el in soup.select(cfg["item_selector"])[:30]:
                t = el.select_one(cfg["title_selector"]) if cfg.get("title_selector") else el
                a = el.select_one(cfg["link_selector"]) if cfg.get("link_selector") else el
                if t and a and a.get("href"):
                    items.append((t.get_text(" ", strip=True), str(httpx.URL(cfg["url"]).join(a["href"])), ""))
        n = _ingest(name, items, cfg) if publish else 0
        ok = bool(items); mark(name, ok, "" if ok else "沒有抓到任何項目(網址/selector 有誤或網站改版?)", n)
        return dict(route=name, points=len(items), best=None, ok=ok, err="" if ok else "沒有抓到任何項目")
    except Exception as e:
        mark(name, False, str(e)); return dict(route=name, points=0, best=None, ok=False, err=str(e)[:150])

# ================= 信用卡條款頁變更監察 =================
async def check_card_pages(client):
    with db() as c: rows = c.execute("SELECT * FROM card_watch").fetchall()
    for r in rows:
        try:
            resp = await client.get(r["url"]); resp.raise_for_status()
            text = re.sub(r"\s+", " ", BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True))
            if len(text) < 200: raise ValueError("頁面文字過少(可能需要瀏覽器渲染),無法監察")
            h = hashlib.sha256(text.encode()).hexdigest()
            with db() as c:
                if not r["last_hash"]:
                    c.execute("UPDATE card_watch SET last_hash=?, last_checked=?, error='' WHERE id=?", (h, now(), r["id"]))
                elif h != r["last_hash"]:
                    c.execute("UPDATE card_watch SET last_hash=?, last_checked=?, changed_at=?, reviewed=0, error='' WHERE id=?", (h, now(), now(), r["id"]))
                else:
                    c.execute("UPDATE card_watch SET last_checked=?, error='' WHERE id=?", (now(), r["id"]))
        except Exception as e:
            with db() as c: c.execute("UPDATE card_watch SET last_checked=?, error=? WHERE id=?", (now(), str(e)[:150], r["id"]))

# ================= 排程 =================
async def run_due():
    routes = get_routes(); t = now()
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}, follow_redirects=True) as client:
        with db() as c:
            provs = c.execute("SELECT * FROM providers WHERE enabled=1").fetchall()
            custom = c.execute("SELECT * FROM custom_sources WHERE enabled=1").fetchall()
        for p in provs:
            if t - p["last_run"] < p["interval_min"] * 60 or p["key"] not in PROVIDERS: continue
            with db() as c: c.execute("UPDATE providers SET last_run=? WHERE key=?", (t, p["key"]))
            try: await PROVIDERS[p["key"]](client, routes, json.loads(p["config"] or "{}"))
            except Exception: log.exception("provider %s failed", p["key"])
        for s in custom:
            if t - s["last_run"] < C.POLL_SECONDS: continue
            with db() as c: c.execute("UPDATE custom_sources SET last_run=? WHERE id=?", (t, s["id"]))
            await run_custom(client, s)
        from . import promo
        await promo.run_due(client)
        if t - int(float(get_setting("cardwatch_last") or 0)) > 6 * 3600:
            from .store import set_setting
            set_setting("cardwatch_last", t); await check_card_pages(client)

async def test_source(kind, key):
    """後台「測試」:不發佈優惠,只回傳覆蓋結果"""
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as client:
        with db() as c:
            if kind == "provider":
                p = c.execute("SELECT * FROM providers WHERE key=?", (key,)).fetchone()
                return await PROVIDERS[p["key"]](client, get_routes(), json.loads(p["config"] or "{}"), publish=False)
            s = c.execute("SELECT * FROM custom_sources WHERE id=?", (key,)).fetchone()
        return [await run_custom(client, s, publish=False)]
