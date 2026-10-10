import asyncio, logging, os, secrets, json, time
from contextlib import asynccontextmanager
from urllib.parse import quote
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
import httpx, re, datetime as dt
from . import config as C, db as DB, sources, bot, mailer, payments, stats, promo
from .store import (add_deal, load_json, wrap_affiliate, locked, now, get_setting, set_setting, on, free_delay, bump,
                    grant_vip, hk_today, get_secret, secret_source, mask, SECRET_ENV, get_routes, seed_defaults)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
T = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
T.env.globals.update(mask=mask, C=C, locked=locked, now=now, on=on, free_delay=free_delay, get_setting=get_setting)
security = HTTPBasic()

async def crawl_loop():
    while True:
        try: await sources.run_due()
        except Exception: log.exception("crawl failed")
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app):
    DB.init(); seed_defaults()
    tasks = [asyncio.create_task(crawl_loop()), asyncio.create_task(mailer.email_loop())]
    if C.BOT_TOKEN: tasks += [asyncio.create_task(bot.poll_loop()), asyncio.create_task(bot.push_loop())]
    else: log.warning("TELEGRAM_BOT_TOKEN 未設定:Bot 與推送停用")
    yield
    for t in tasks: t.cancel()

app = FastAPI(title="著數飛", lifespan=lifespan)

PV_PATHS = {"/", "/flights", "/cards", "/vip"}
BOT_RE = re.compile(r"bot|spider|crawl|slurp|preview|monitor|uptime", re.I)

@app.middleware("http")
async def pageviews(request: Request, call_next):
    resp = await call_next(request)
    try:
        if request.method == "GET" and request.url.path in PV_PATHS and resp.status_code == 200 \
                and not BOT_RE.search(request.headers.get("user-agent", "")):
            bump("pv"); bump("pv:" + request.url.path)
    except Exception: pass
    return resp

def msg(request, text, status=200):
    return T.TemplateResponse(request, "message.html", dict(text=text), status_code=status)

def admin(cred: HTTPBasicCredentials = Depends(security)):
    ok = C.ADMIN_PASSWORD and secrets.compare_digest(cred.password, C.ADMIN_PASSWORD) and secrets.compare_digest(cred.username, C.ADMIN_USER)
    if not ok: raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

@app.get("/healthz")
def healthz(): return PlainTextResponse("ok")

@app.get("/")
def index(request: Request, kind: str = "", dest: str = ""):
    q, p = "SELECT * FROM deals WHERE (expires_at IS NULL OR expires_at>?)", [now()]
    if kind: q += " AND kind=?"; p.append(kind)
    if dest: q += " AND dest=?"; p.append(dest.upper())
    q += " ORDER BY created_at DESC LIMIT 60"
    with DB.db() as c:
        deals = c.execute(q, p).fetchall()
        dests = [r["dest"] for r in c.execute("SELECT DISTINCT dest FROM deals WHERE dest!='' ORDER BY dest")]
    return T.TemplateResponse(request, "index.html", dict(deals=deals, kind=kind, dest=dest.upper(), dests=dests))

@app.get("/flights")
def flights(request: Request):
    with DB.db() as c:
        rows = c.execute("""SELECT p.* FROM price_history p JOIN
            (SELECT origin,dest,MIN(price) mp FROM price_history WHERE ts>? GROUP BY origin,dest) m
            ON p.origin=m.origin AND p.dest=m.dest AND p.price=m.mp WHERE p.ts>? GROUP BY p.origin,p.dest ORDER BY p.price""",
            (now() - 86400, now() - 86400)).fetchall()
    return T.TemplateResponse(request, "flights.html", dict(rows=rows))

def _cards():
    data = None
    raw = get_setting("cards_json")
    if raw:
        try: data = json.loads(raw)
        except Exception: data = None
    if data is None: data = load_json("cards.json", None)
    if data is None:
        with open(os.path.join(os.path.dirname(__file__), "cards_default.json"), encoding="utf-8") as f: data = json.load(f)
    return data

def calc(data, category, amount):
    out = []
    for cd in data["cards"]:
        rt = cd["rates"].get(category) or cd["rates"].get("other", {"rate": 0})
        cap = rt.get("cap_spend") or amount
        main = min(amount, cap) * rt["rate"]
        rest = max(amount - cap, 0) * cd["rates"].get("other", {"rate": 0})["rate"]
        out.append(dict(card=cd, reward=round(main + rest, 1), rate=rt["rate"]))
    return sorted(out, key=lambda x: -x["reward"])

def _cards_is_real():
    return bool(get_setting("cards_json")) or os.path.exists(os.path.join(C.DATA_DIR, "cards.json"))

@app.get("/cards")
def cards(request: Request, category: str = "overseas", amount: float = 10000):
    if not _cards_is_real():   # 未填真實資料時不公開示例數據
        return msg(request, "信用卡回贈計算機資料整理中,稍後上線。")
    data = _cards()
    return T.TemplateResponse(request, "cards.html", dict(data=data, category=category, amount=amount,
                                                           results=calc(data, category, max(amount, 0))))

@app.get("/vip")
def vip(request: Request, tg: str = ""):
    return T.TemplateResponse(request, "vip.html", dict(tg=tg if tg.lstrip("-").isdigit() else "",
        stripe=on("stripe_enabled") and bool(get_secret("stripe_secret")), manual=on("manual_pay_enabled"),
        pm=get_setting("price_monthly"), py=get_setting("price_yearly"), manual_text=get_setting("manual_pay_text")))

def _ident(tg, email):
    tg = int(tg) if tg and tg.strip().lstrip("-").isdigit() else None
    email = email.strip().lower() if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()) else None
    return tg, email

@app.post("/vip/checkout")
async def vip_checkout(request: Request, plan: str = Form("monthly"), tg: str = Form(""), email: str = Form("")):
    if not (on("stripe_enabled") and get_secret("stripe_secret")): return msg(request, "信用卡付款暫未開放。", 400)
    if plan not in payments.PLANS: return msg(request, "無效方案。", 400)
    tg, email = _ident(tg, email)
    if not tg and not email: return msg(request, "請填寫 Telegram chat_id 或 Email 其中一項,用作開通 VIP。", 400)
    try: url = await payments.create_checkout(plan, tg, email)
    except Exception as e:
        log.error("checkout failed: %s", e); return msg(request, "建立付款連結失敗,請稍後再試或聯絡管理員。", 502)
    return RedirectResponse(url, 303)

@app.post("/vip/manual")
def vip_manual(request: Request, plan: str = Form("monthly"), tg: str = Form(""), email: str = Form(""), note: str = Form("")):
    if not on("manual_pay_enabled"): return msg(request, "手動付款登記暫未開放。", 400)
    tg, email = _ident(tg, email)
    if not tg and not email: return msg(request, "請填寫 Telegram chat_id 或 Email 其中一項。", 400)
    _, days, _ = payments.PLANS.get(plan, payments.PLANS["monthly"])
    payments._record("manual", "m-" + secrets.token_hex(6), tg, email or "", payments.plan_price(plan if plan in payments.PLANS else "monthly"),
                     days, "pending", note[:200])
    return msg(request, "已收到你的付款登記,管理員核實後會為你開通 VIP(Telegram 用 /me 查看狀態)。")

@app.get("/vip/thanks")
def vip_thanks(request: Request):
    return msg(request, "多謝支持!付款確認後 VIP 會自動開通(通常數秒內),可用 Telegram /me 查看。")

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    body = await request.body()
    if not payments.verify_signature(body, request.headers.get("stripe-signature", ""), get_secret("stripe_webhook_secret")):
        raise HTTPException(400, "bad signature")
    ev = json.loads(body)
    try:
        need_confirm = await payments.handle_event(ev)
    except Exception:
        log.exception("webhook failed"); raise HTTPException(500)   # 回 500 讓 Stripe 重送
    if need_confirm and on("email_enabled"):
        with DB.db() as c: tok = c.execute("SELECT token FROM email_subs WHERE email=?", (need_confirm,)).fetchone()["token"]
        subj, html_ = mailer.confirm_mail(tok)
        async with httpx.AsyncClient(timeout=20) as cl: await mailer.send_email(cl, need_confirm, subj, html_)
    return {"ok": True}

# ---------- Email 訂閱 ----------
@app.post("/subscribe")
async def subscribe(request: Request, email: str = Form(...)):
    if not on("email_enabled"): return msg(request, "Email 訂閱暫未開放。", 400)
    _, em = _ident("", email)
    if not em: return msg(request, "Email 格式不正確。", 400)
    with DB.db() as c:
        c.execute("INSERT OR IGNORE INTO email_subs(email,token,created_at) VALUES(?,?,?)", (em, secrets.token_urlsafe(24), now()))
        row = c.execute("SELECT * FROM email_subs WHERE email=?", (em,)).fetchone()
    if not row["confirmed"]:
        subj, html_ = mailer.confirm_mail(row["token"])
        async with httpx.AsyncClient(timeout=20) as cl: await mailer.send_email(cl, em, subj, html_)
    return msg(request, "已發送確認郵件,請到信箱按確認連結(未確認不會收到優惠郵件)。")

@app.get("/email/confirm/{token}")
def email_confirm(request: Request, token: str):
    with DB.db() as c:
        cur = c.execute("UPDATE email_subs SET confirmed=1 WHERE token=?", (token,))
    return msg(request, "✅ 訂閱已確認。" if cur.rowcount else "連結無效。", 200 if cur.rowcount else 404)

@app.api_route("/email/unsub/{token}", methods=["GET", "POST"])
def email_unsub(request: Request, token: str):
    with DB.db() as c:
        row = c.execute("SELECT vip_until FROM email_subs WHERE token=?", (token,)).fetchone()
        c.execute("UPDATE email_subs SET confirmed=0 WHERE token=?", (token,))
    return msg(request, "已取消訂閱,不會再收到郵件。" if row else "連結無效。", 200 if row else 404)

@app.get("/go/{deal_id}")
def go(deal_id: int, request: Request):
    with DB.db() as c:
        d = c.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not d: raise HTTPException(404)
        c.execute("INSERT INTO clicks(target,ts,ref) VALUES(?,?,?)", (f"deal:{deal_id}", now(), request.headers.get("referer", "")[:200]))
    return RedirectResponse(wrap_affiliate(d["url"], d["merchant"]), 302)

@app.get("/go/card/{card_id}")
def go_card(card_id: str, request: Request):
    cd = next((x for x in _cards()["cards"] if x["id"] == card_id), None)
    if not cd: raise HTTPException(404)
    with DB.db() as c:
        c.execute("INSERT INTO clicks(target,ts,ref) VALUES(?,?,?)", (f"card:{card_id}", now(), ""))
    return RedirectResponse(wrap_affiliate(cd["apply_url"], cd["bank"]), 302)

@app.get("/api/deals")
def api_deals(limit: int = 30):
    with DB.db() as c:
        rows = c.execute("SELECT id,kind,title,summary,dest,price,created_at FROM deals ORDER BY created_at DESC LIMIT ?", (min(limit, 100),)).fetchall()
    return JSONResponse([dict(r) for r in rows if not locked(r["created_at"])])

# ---------- 後台 ----------
def _paid_days(plan): return payments.PLANS[plan][1]

@app.get("/admin", dependencies=[Depends(admin)])
def admin_dash(request: Request):
    with DB.db() as c:
        n_deals = c.execute("SELECT COUNT(*) n FROM deals").fetchone()["n"]
        n_prov = c.execute("SELECT COUNT(*) n FROM providers WHERE enabled=1").fetchone()["n"]
        n_src = c.execute("SELECT COUNT(*) n FROM custom_sources WHERE enabled=1").fetchone()["n"] + c.execute("SELECT COUNT(*) n FROM promo_watch WHERE enabled=1").fetchone()["n"]
    checklist = [
        ("發佈至少 3 條真實優惠(「🔥 優惠」手動新增,用自己的文字、連到官方頁)", n_deals >= 3, "/admin/deals"),
        ("設定 Telegram Bot(.env 的 TELEGRAM_BOT_TOKEN 與 TELEGRAM_BOT_USERNAME)", bool(C.BOT_TOKEN and C.BOT_USERNAME), None),
        ("啟用至少一個票價來源並測試航線覆蓋", n_prov >= 1, "/admin/sources"),
        ("新增優惠來源(RSS/爬蟲/促銷頁監察)", n_src >= 1, "/admin/promo"),
        ("填入真實信用卡回贈資料(否則 /cards 不公開)", _cards_is_real(), "/admin/cards"),
        ("設定聯盟連結模板(data/affiliate.json)", bool(load_json("affiliate.json", {})), None),
        ("修改後台密碼(ADMIN_PASSWORD)", bool(C.ADMIN_PASSWORD) and "請改成" not in C.ADMIN_PASSWORD, None),
        ("私隱政策與條款已由專業人士審閱並在後台確認", get_setting("legal_draft") == "0", "/admin/settings"),
        ("(收費前)Resend / Stripe 設定並測試", bool(get_secret("resend_api_key")) and bool(get_secret("stripe_secret")), "/admin/integrations"),
    ]
    return T.TemplateResponse(request, "admin_dash.html", dict(d=stats.dashboard(), tab="dash", checklist=checklist))

@app.get("/admin/deals", dependencies=[Depends(admin)])
def admin_deals(request: Request):
    with DB.db() as c:
        deals = c.execute("""SELECT d.*, (SELECT COUNT(*) FROM clicks WHERE target='deal:'||d.id) clicks
                             FROM deals d ORDER BY created_at DESC LIMIT 60""").fetchall()
    return T.TemplateResponse(request, "admin_deals.html", dict(deals=deals, tab="deals"))

@app.get("/admin/settings", dependencies=[Depends(admin)])
def admin_settings(request: Request):
    with DB.db() as c:
        pend = c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY ts DESC").fetchall()
        paid = c.execute("SELECT * FROM payments WHERE status='paid' ORDER BY ts DESC LIMIT 15").fetchall()
        rev = c.execute("SELECT * FROM revenue ORDER BY day DESC, id DESC LIMIT 15").fetchall()
    ready = dict(stripe=bool(get_secret("stripe_secret") and get_secret("stripe_webhook_secret")), resend=bool(get_secret("resend_api_key")), bot=bool(C.BOT_TOKEN), tp=bool(get_secret("tp_token") or get_secret("ignav_key")))
    return T.TemplateResponse(request, "admin_settings.html", dict(pend=pend, paid=paid, rev=rev, ready=ready, tab="settings", today=hk_today()))

@app.post("/admin/settings", dependencies=[Depends(admin)])
def admin_settings_save(email_enabled: str = Form(""), stripe_enabled: str = Form(""), manual_pay_enabled: str = Form(""),
                        price_monthly: float = Form(...), price_yearly: float = Form(...), free_delay_min: int = Form(...),
                        manual_pay_text: str = Form(""), site_notice: str = Form(""), operator_name: str = Form(""), contact_email: str = Form(""),
                        legal_reviewed: str = Form("")):
    for k, v in dict(operator_name=operator_name.strip(), contact_email=contact_email.strip(), legal_draft="0" if legal_reviewed else "1", email_enabled="1" if email_enabled else "0", stripe_enabled="1" if stripe_enabled else "0",
                     manual_pay_enabled="1" if manual_pay_enabled else "0", price_monthly=price_monthly, price_yearly=price_yearly,
                     free_delay_min=max(free_delay_min, 0), manual_pay_text=manual_pay_text, site_notice=site_notice).items():
        set_setting(k, v)
    return RedirectResponse("/admin/settings", 303)

@app.post("/admin/pay/{pid}/{action}", dependencies=[Depends(admin)])
async def admin_pay(pid: int, action: str):
    with DB.db() as c:
        p = c.execute("SELECT * FROM payments WHERE id=? AND status='pending'", (pid,)).fetchone()
        if p: c.execute("UPDATE payments SET status=? WHERE id=?", ("paid" if action == "approve" else "rejected", pid))
    if p and action == "approve":
        need = grant_vip(p["days"], p["tg"], p["email"] or None)
        if need and on("email_enabled"):
            with DB.db() as c: tok = c.execute("SELECT token FROM email_subs WHERE email=?", (need,)).fetchone()["token"]
            subj, html_ = mailer.confirm_mail(tok)
            async with httpx.AsyncClient(timeout=20) as cl: await mailer.send_email(cl, need, subj, html_)
    return RedirectResponse("/admin/settings", 303)

@app.post("/admin/revenue", dependencies=[Depends(admin)])
def admin_revenue(day: str = Form(...), source: str = Form(...), amount: float = Form(...), note: str = Form("")):
    with DB.db() as c: c.execute("INSERT INTO revenue(day,source,amount,note) VALUES(?,?,?,?)", (day, source.strip().lower(), amount, note[:200]))
    return RedirectResponse("/admin/settings", 303)

@app.post("/admin/deal", dependencies=[Depends(admin)])
def admin_add(kind: str = Form("promo"), title: str = Form(...), url: str = Form(...), summary: str = Form(""),
              merchant: str = Form(""), dest: str = Form(""), price: str = Form(""), code: str = Form("")):
    add_deal(kind, title, url, summary, merchant.lower(), dest=dest.upper(), price=float(price) if price else None, code=code, source="manual")
    return RedirectResponse("/admin/deals", 303)

@app.post("/admin/deal/{deal_id}/delete", dependencies=[Depends(admin)])
def admin_del(deal_id: int):
    with DB.db() as c: c.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    return RedirectResponse("/admin/deals", 303)

@app.post("/admin/vip", dependencies=[Depends(admin)])
def admin_vip(chat_id: str = Form(""), email: str = Form(""), days: int = Form(...)):
    tg, em = _ident(chat_id, email)
    if tg or em: grant_vip(days, tg, em)
    return RedirectResponse("/admin/settings", 303)


# ================= 後台:數據來源 =================
def _pj(txt, default):
    try: return json.loads(txt)
    except Exception: return default

@app.get("/admin/sources", dependencies=[Depends(admin)])
def admin_sources(request: Request):
    return _sources_page(request)

def _sources_page(request, test_rows=None, test_name=""):
    with DB.db() as c:
        provs = c.execute("SELECT * FROM providers ORDER BY key").fetchall()
        routes = c.execute("SELECT * FROM routes ORDER BY id").fetchall()
        custom = c.execute("SELECT * FROM custom_sources ORDER BY id").fetchall()
    return T.TemplateResponse(request, "admin_sources.html", dict(tab="sources", provs=provs, routes=routes, custom=custom,
        cfgs={p["key"]: p["config"] for p in provs}, test_rows=test_rows, test_name=test_name,
        keys=dict(tp=bool(get_secret("tp_token")), ignav=bool(get_secret("ignav_key")), serp=bool(get_secret("serpapi_key")))))

@app.post("/admin/sources/provider/{key}", dependencies=[Depends(admin)])
def admin_provider_save(key: str, enabled: str = Form(""), interval_min: int = Form(...), config: str = Form("{}")):
    cfg = _pj(config, None)
    if cfg is None: raise HTTPException(400, "config 不是有效 JSON")
    with DB.db() as c:
        c.execute("UPDATE providers SET enabled=?, interval_min=?, config=? WHERE key=?", (1 if enabled else 0, max(interval_min, 5), json.dumps(cfg, ensure_ascii=False), key))
    return RedirectResponse("/admin/sources", 303)

@app.post("/admin/sources/test/{kind}/{key}", dependencies=[Depends(admin)])
async def admin_source_test(request: Request, kind: str, key: str):
    rows = await sources.test_source(kind, key)
    return _sources_page(request, test_rows=rows, test_name=f"{kind}:{key}")

@app.post("/admin/sources/route", dependencies=[Depends(admin)])
def admin_route_add(origin: str = Form(...), dest: str = Form(...)):
    o, d = origin.strip().upper(), dest.strip().upper()
    if len(o) == 3 and len(d) == 3 and o.isalpha() and d.isalpha():
        with DB.db() as c: c.execute("INSERT OR IGNORE INTO routes(origin,dest) VALUES(?,?)", (o, d))
    return RedirectResponse("/admin/sources", 303)

@app.post("/admin/sources/route/{rid}/{action}", dependencies=[Depends(admin)])
def admin_route_act(rid: int, action: str):
    with DB.db() as c:
        if action == "delete": c.execute("DELETE FROM routes WHERE id=?", (rid,))
        else: c.execute("UPDATE routes SET enabled=1-enabled WHERE id=?", (rid,))
    return RedirectResponse("/admin/sources", 303)

@app.post("/admin/sources/custom", dependencies=[Depends(admin)])
def admin_custom_add(kind: str = Form(...), name: str = Form(...), url: str = Form(...), merchant: str = Form(""),
                     deal_kind: str = Form("promo"), item_selector: str = Form(""), title_selector: str = Form(""), link_selector: str = Form("")):
    cfg = dict(url=url.strip(), merchant=merchant.strip().lower(), kind=deal_kind)
    if kind == "scrape": cfg.update(item_selector=item_selector, title_selector=title_selector, link_selector=link_selector)
    with DB.db() as c: c.execute("INSERT OR IGNORE INTO custom_sources(kind,name,config) VALUES(?,?,?)", (kind, name.strip(), json.dumps(cfg, ensure_ascii=False)))
    return RedirectResponse("/admin/sources", 303)

@app.post("/admin/sources/custom/{sid}/{action}", dependencies=[Depends(admin)])
def admin_custom_act(sid: int, action: str):
    with DB.db() as c:
        if action == "delete": c.execute("DELETE FROM custom_sources WHERE id=?", (sid,))
        else: c.execute("UPDATE custom_sources SET enabled=1-enabled WHERE id=?", (sid,))
    return RedirectResponse("/admin/sources", 303)

@app.post("/admin/sources/params", dependencies=[Depends(admin)])
def admin_params(keywords: str = Form(""), deal_drop_ratio: float = Form(0.85), min_history: int = Form(8)):
    set_setting("keywords", keywords); set_setting("deal_drop_ratio", min(max(deal_drop_ratio, 0.3), 0.99)); set_setting("min_history", max(min_history, 1))
    return RedirectResponse("/admin/sources", 303)

# ================= 後台:信用卡資料 =================
@app.get("/admin/cards", dependencies=[Depends(admin)])
def admin_cards(request: Request, err: str = ""):
    with DB.db() as c: watch = c.execute("SELECT * FROM card_watch ORDER BY reviewed, id").fetchall()
    return T.TemplateResponse(request, "admin_cards.html", dict(tab="cards", watch=watch, err=err,
        cards_json=json.dumps(_cards(), ensure_ascii=False, indent=2), custom=bool(get_setting("cards_json"))))

@app.post("/admin/cards", dependencies=[Depends(admin)])
def admin_cards_save(cards_json: str = Form(...), action: str = Form("save")):
    if action == "reset":
        set_setting("cards_json", ""); return RedirectResponse("/admin/cards", 303)
    try:
        d = json.loads(cards_json)
        assert isinstance(d.get("categories"), dict) and isinstance(d.get("cards"), list)
        for cd in d["cards"]:
            assert all(k in cd for k in ("id", "name", "bank", "apply_url", "rates")) and isinstance(cd["rates"], dict)
            for k, v in cd["rates"].items(): assert isinstance(v.get("rate"), (int, float)) and 0 <= v["rate"] <= 1
    except Exception as e:
        return RedirectResponse("/admin/cards?err=" + quote(f"JSON 格式/欄位有誤:{e}"), 303)
    set_setting("cards_json", json.dumps(d, ensure_ascii=False))
    return RedirectResponse("/admin/cards", 303)

@app.post("/admin/cards/watch", dependencies=[Depends(admin)])
def admin_watch_add(name: str = Form(...), url: str = Form(...)):
    with DB.db() as c: c.execute("INSERT INTO card_watch(name,url) VALUES(?,?)", (name.strip(), url.strip()))
    return RedirectResponse("/admin/cards", 303)

@app.post("/admin/cards/watch/{wid}/{action}", dependencies=[Depends(admin)])
def admin_watch_act(wid: int, action: str):
    with DB.db() as c:
        if action == "delete": c.execute("DELETE FROM card_watch WHERE id=?", (wid,))
        else: c.execute("UPDATE card_watch SET reviewed=1 WHERE id=?", (wid,))
    return RedirectResponse("/admin/cards", 303)

# ================= 後台:整合設定(密鑰) =================
SECRET_LABELS = [("resend_api_key", "Resend API Key"), ("mail_from", "寄件人(MAIL_FROM,需為已驗證域名)"),
                 ("tp_token", "Travelpayouts token"), ("tp_marker", "Travelpayouts marker"), ("ignav_key", "Ignav API key"), ("serpapi_key", "SerpApi API key"), ("anthropic_key", "Anthropic API Key(促銷頁 AI 抽取用)"),
                 ("stripe_secret", "Stripe Secret Key"), ("stripe_webhook_secret", "Stripe Webhook 簽名密鑰")]

@app.get("/admin/integrations", dependencies=[Depends(admin)])
def admin_integrations(request: Request, flash: str = ""):
    rows = [dict(name=n, label=l, src=secret_source(n), shown=(get_secret(n) if n == "mail_from" else mask(get_secret(n)))) for n, l in SECRET_LABELS]
    return T.TemplateResponse(request, "admin_integrations.html", dict(tab="integ", rows=rows, flash=flash, digest_hour=get_setting("digest_hour"),
                                                                       webhook=C.BASE_URL + "/stripe/webhook"))

@app.post("/admin/integrations", dependencies=[Depends(admin)])
async def admin_integrations_save(request: Request):
    form = await request.form()
    for name, _ in SECRET_LABELS:
        v = (form.get(name) or "").strip()
        if v == "-": set_setting("secret:" + name, "")
        elif v: set_setting("secret:" + name, v)
    h = (form.get("digest_hour") or "").strip()
    if h.isdigit() and 0 <= int(h) <= 23: set_setting("digest_hour", h)
    return RedirectResponse("/admin/integrations?flash=" + quote("已儲存(留空 = 不變;填 - = 清除後台值,改用環境變數)"), 303)

@app.post("/admin/integrations/test-email", dependencies=[Depends(admin)])
async def admin_test_email(to: str = Form(...)):
    async with httpx.AsyncClient(timeout=20) as cl:
        ok = await mailer.send_email(cl, to.strip(), "著數飛測試郵件", "<p>如果你收到這封郵件,Resend 設定正確 ✅</p>")
    return RedirectResponse("/admin/integrations?flash=" + quote("測試郵件已送出,請查收" if ok else "發送失敗:請檢查 Resend key / 寄件域名驗證(見容器日誌)"), 303)


# ================= 法律頁 =================
@app.get("/privacy")
def privacy(request: Request): return T.TemplateResponse(request, "privacy.html", dict(today=hk_today()))

@app.get("/terms")
def terms(request: Request): return T.TemplateResponse(request, "terms.html", dict(today=hk_today()))

# ================= 取消訂閱自助(Stripe Customer Portal) =================
@app.get("/vip/manage")
def manage_form(request: Request):
    return T.TemplateResponse(request, "manage.html", dict(sent=False, can=on("stripe_enabled") and bool(get_secret("stripe_secret"))))

@app.post("/vip/manage")
async def manage_send(request: Request, email: str = Form(...)):
    _, em = _ident("", email)
    if em and on("stripe_enabled") and get_secret("stripe_secret"):
        with DB.db() as c:
            row = c.execute("SELECT customer_id FROM stripe_customers WHERE email=? LIMIT 1", (em,)).fetchone()
            if row:
                tok = secrets.token_urlsafe(32)
                c.execute("DELETE FROM magic_tokens WHERE expires<?", (now(),))
                c.execute("INSERT INTO magic_tokens(token,customer_id,expires) VALUES(?,?,?)", (tok, row["customer_id"], now() + 1800))
        if row:
            url = f"{C.BASE_URL}/vip/manage/{tok}"
            body = (f"<p>請在 30 分鐘內按下面連結管理或取消你的著數飛 VIP 訂閱:</p><p><a href=\"{url}\">{url}</a></p>"
                    "<p>如非你本人操作,請忽略此郵件。</p>")
            async with httpx.AsyncClient(timeout=20) as cl:
                await mailer.send_email(cl, em, "著數飛|管理訂閱連結", body)
    # 無論有否紀錄都回同一訊息,避免洩漏誰是訂戶
    return T.TemplateResponse(request, "manage.html", dict(sent=True, can=True))

@app.get("/vip/manage/{token}")
async def manage_open(request: Request, token: str):
    with DB.db() as c:
        row = c.execute("SELECT * FROM magic_tokens WHERE token=? AND expires>?", (token, now())).fetchone()
    if not row: return msg(request, "連結無效或已過期,請重新申請。", 404)
    try: url = await payments.create_portal(row["customer_id"])
    except Exception as e:
        log.error("portal failed: %s", e); return msg(request, "暫時無法開啟管理頁面(可能尚未在 Stripe 啟用 Customer portal),請聯絡我們。", 502)
    return RedirectResponse(url, 303)

# ================= 後台:優惠收件匣(RSS/爬蟲先審核再發佈) =================
@app.get("/admin/inbox", dependencies=[Depends(admin)])
def admin_inbox(request: Request):
    with DB.db() as c: rows = c.execute("SELECT * FROM inbox WHERE status='new' ORDER BY ts DESC LIMIT 80").fetchall()
    return T.TemplateResponse(request, "admin_inbox.html", dict(tab="inbox", rows=rows))

@app.post("/admin/inbox/{iid}/publish", dependencies=[Depends(admin)])
def admin_inbox_publish(iid: int, title: str = Form(...), summary: str = Form(""), merchant: str = Form(""), dest: str = Form(""),
                        code: str = Form(""), url: str = Form(...), kind: str = Form("promo"), price: str = Form(""), expires_at: str = Form("")):
    add_deal(kind, title.strip(), url.strip(), summary.strip(), merchant.strip().lower(), dest=dest.strip().upper(), code=code.strip(), source="inbox",
             price=float(price) if price else None, expires_at=int(expires_at) if expires_at.isdigit() else None)
    with DB.db() as c: c.execute("UPDATE inbox SET status='published' WHERE id=?", (iid,))
    return RedirectResponse("/admin/inbox", 303)

@app.post("/admin/inbox/{iid}/ignore", dependencies=[Depends(admin)])
def admin_inbox_ignore(iid: int):
    with DB.db() as c: c.execute("UPDATE inbox SET status='ignored' WHERE id=?", (iid,))
    return RedirectResponse("/admin/inbox", 303)


# ================= 後台:促銷頁監察(AI 抽取) =================
def _promo_page(request, report=None, report_for=None, flash=""):
    with DB.db() as c: ws = c.execute("SELECT * FROM promo_watch ORDER BY id").fetchall()
    return T.TemplateResponse(request, "admin_promo.html", dict(tab="promo", ws=ws, report=report, report_for=report_for, flash=flash,
        has_key=bool(get_secret("anthropic_key")), now=now))

@app.get("/admin/promo", dependencies=[Depends(admin)])
def admin_promo(request: Request): return _promo_page(request)

@app.post("/admin/promo", dependencies=[Depends(admin)])
def admin_promo_add(name: str = Form(...), url: str = Form(...), merchant: str = Form(""), mode: str = Form("review"), interval_hours: int = Form(6)):
    with DB.db() as c:
        c.execute("INSERT INTO promo_watch(name,url,merchant,mode,interval_hours) VALUES(?,?,?,?,?)",
                  (name.strip(), url.strip(), merchant.strip().lower(), "auto" if mode == "auto" else "review", max(interval_hours, 1)))
    return RedirectResponse("/admin/promo", 303)

@app.post("/admin/promo/settings", dependencies=[Depends(admin)])
def admin_promo_settings(ai_auto_publish: str = Form(""), ai_model: str = Form("claude-haiku-5-5"), ai_min_confidence: float = Form(0.75),
                         ai_daily_cap: int = Form(20), ai_origins: str = Form("HKG")):
    set_setting("ai_auto_publish", "1" if ai_auto_publish else "0"); set_setting("ai_model", ai_model.strip() or "claude-haiku-5-5")
    set_setting("ai_min_confidence", min(max(ai_min_confidence, 0.3), 1)); set_setting("ai_daily_cap", max(ai_daily_cap, 0)); set_setting("ai_origins", ai_origins.strip().upper() or "HKG")
    return RedirectResponse("/admin/promo", 303)

@app.post("/admin/promo/{wid}/test", dependencies=[Depends(admin)])
async def admin_promo_test(request: Request, wid: int):
    with DB.db() as c: w = c.execute("SELECT * FROM promo_watch WHERE id=?", (wid,)).fetchone()
    if not w: raise HTTPException(404)
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as cl:
        rep = await promo.check_watch(cl, w, dry=True)
    return _promo_page(request, report=rep, report_for=w["name"])

@app.post("/admin/promo/{wid}/run", dependencies=[Depends(admin)])
async def admin_promo_run(request: Request, wid: int):
    with DB.db() as c: w = c.execute("SELECT * FROM promo_watch WHERE id=?", (wid,)).fetchone()
    if not w: raise HTTPException(404)
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as cl:
        rep = await promo.check_watch(cl, w, dry=False)
    return _promo_page(request, report=rep, report_for=w["name"] + "(已實際執行)")

@app.post("/admin/promo/{wid}/{action}", dependencies=[Depends(admin)])
def admin_promo_act(wid: int, action: str):
    with DB.db() as c:
        if action == "delete": c.execute("DELETE FROM promo_watch WHERE id=?", (wid,))
        elif action == "toggle": c.execute("UPDATE promo_watch SET enabled=1-enabled WHERE id=?", (wid,))
        elif action == "mode": c.execute("UPDATE promo_watch SET mode=CASE mode WHEN 'auto' THEN 'review' ELSE 'auto' END WHERE id=?", (wid,))
    return RedirectResponse("/admin/promo", 303)
