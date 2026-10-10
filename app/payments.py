import hmac, hashlib, time, json, logging
import httpx
from . import config as C
from .db import db
from .store import now, get_setting, grant_vip, get_secret

log = logging.getLogger("pay")
PLANS = {"monthly": ("month", 31, "月費"), "yearly": ("year", 366, "年費")}

def plan_price(plan): return float(get_setting("price_" + plan) or 0)

async def create_checkout(plan, tg, email):
    interval, days, label = PLANS[plan]
    amount = int(round(plan_price(plan) * 100))
    meta = {"tg": str(tg or ""), "email": email or "", "days": str(days), "plan": plan}
    data = {"mode": "subscription", "success_url": C.BASE_URL + "/vip/thanks", "cancel_url": C.BASE_URL + "/vip",
            "line_items[0][quantity]": 1, "line_items[0][price_data][currency]": "hkd",
            "line_items[0][price_data][unit_amount]": amount,
            "line_items[0][price_data][recurring][interval]": interval,
            "line_items[0][price_data][product_data][name]": f"著數飛 VIP({label})"}
    for k, v in meta.items():
        data[f"metadata[{k}]"] = v; data[f"subscription_data[metadata][{k}]"] = v
    if email: data["customer_email"] = email
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.stripe.com/v1/checkout/sessions", data=data, auth=(get_secret("stripe_secret"), ""))
    if r.status_code >= 300: raise RuntimeError(f"stripe {r.status_code}: {r.text[:300]}")
    return r.json()["url"]

def verify_signature(payload: bytes, header: str, secret: str, tolerance=300):
    if not secret or not header: return False
    ts, sigs = None, []
    for part in header.split(","):
        k, _, v = part.partition("=")
        if k == "t": ts = v
        elif k == "v1": sigs.append(v)
    if not ts or not ts.isdigit() or abs(time.time() - int(ts)) > tolerance: return False
    exp = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(exp, s) for s in sigs)

def _record(provider, ref, tg, email, amount, days, status, note=""):
    with db() as c:
        cur = c.execute("INSERT OR IGNORE INTO payments(provider,ref,tg,email,amount,days,status,note,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                        (provider, ref, tg, email, amount, days, status, note, now()))
        return cur.rowcount == 1

async def handle_event(ev):
    """回傳需要發確認信的 email(若有)"""
    typ, obj = ev.get("type"), ev.get("data", {}).get("object", {})
    meta, amount = None, 0
    if typ == "checkout.session.completed" and obj.get("payment_status") == "paid":
        meta, amount = obj.get("metadata") or {}, (obj.get("amount_total") or 0) / 100
    elif typ == "invoice.paid" and obj.get("billing_reason") != "subscription_create":
        sub_id = obj.get("subscription") or ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if sub_id:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"https://api.stripe.com/v1/subscriptions/{sub_id}", auth=(get_secret("stripe_secret"), ""))
            if r.status_code < 300: meta, amount = r.json().get("metadata") or {}, (obj.get("amount_paid") or 0) / 100
    if not meta: return None
    _remember_customer(obj, meta)
    tg = int(meta["tg"]) if str(meta.get("tg", "")).lstrip("-").isdigit() else None
    email = meta.get("email") or None
    days = int(meta.get("days") or 31)
    if not _record("stripe", ev["id"], tg, email or "", amount, days, "paid", typ):
        return None        # 已處理過(Stripe 會重送)
    return grant_vip(days, tg, email)


def _remember_customer(obj, meta):
    cid = obj.get("customer")
    if not cid or not isinstance(cid, str): return
    emails = {e.strip().lower() for e in (meta.get("email"), (obj.get("customer_details") or {}).get("email"), obj.get("customer_email")) if e}
    tg = int(meta["tg"]) if str(meta.get("tg", "")).lstrip("-").isdigit() else None
    with db() as c:
        for em in emails or {""}:
            c.execute("INSERT OR IGNORE INTO stripe_customers(customer_id,email,tg) VALUES(?,?,?)", (cid, em, tg))
        if tg: c.execute("UPDATE stripe_customers SET tg=? WHERE customer_id=? AND tg IS NULL", (tg, cid))

async def create_portal(customer_id, client=None):
    """Stripe 客戶入口(取消/更改付款方式)。需先在 Stripe 後台啟用 Customer portal。"""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.post("https://api.stripe.com/v1/billing_portal/sessions", data={"customer": customer_id, "return_url": C.BASE_URL + "/vip"},
                              auth=(get_secret("stripe_secret"), ""))
        if r.status_code >= 300: raise RuntimeError(f"stripe {r.status_code}: {r.text[:200]}")
        return r.json()["url"]
    finally:
        if own: await client.aclose()
