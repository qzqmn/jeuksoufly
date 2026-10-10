import asyncio, html, logging, datetime as dt
import httpx
from . import config as C
from .db import db
from .store import now, on, locked, free_delay, hk_today, HKT, get_secret, get_setting

log = logging.getLogger("mail")

async def send_email(client, to, subject, body_html, unsub_url=None):
    key = get_secret("resend_api_key")
    if not key:
        log.warning("RESEND_API_KEY 未設定,未發送 Email:%s", subject); return False
    payload = {"from": get_secret("mail_from"), "to": [to], "subject": subject, "html": body_html}
    if unsub_url: payload["headers"] = {"List-Unsubscribe": f"<{unsub_url}>"}
    try:
        r = await client.post("https://api.resend.com/emails", json=payload, headers={"Authorization": f"Bearer {key}"})
        if r.status_code >= 300: log.warning("resend %s: %s", r.status_code, r.text[:200])
        return r.status_code < 300
    except Exception as e:
        log.warning("resend failed: %s", e); return False

def confirm_mail(token):
    url = f"{C.BASE_URL}/email/confirm/{token}"
    return "確認訂閱「著數飛」", f'<p>請按下面連結確認訂閱(未確認不會收到任何郵件):</p><p><a href="{url}">{url}</a></p><p>如非你本人操作,請忽略此郵件。</p>'

def _deal_li(d):
    extra = f"<br><small>{html.escape(d['summary'])}</small>" if d["summary"] else ""
    code = f" · 優惠碼 <b>{html.escape(d['code'])}</b>" if d["code"] else ""
    return f'<li style="margin:10px 0"><a href="{C.BASE_URL}/go/{d["id"]}"><b>{html.escape(d["title"])}</b></a>{code}{extra}</li>'

def wrap(deals, token, head):
    unsub = f"{C.BASE_URL}/email/unsub/{token}"
    foot = (f'<hr><small>你收到此郵件因為你訂閱了著數飛。<a href="{unsub}">一鍵取消訂閱</a>。'
            "部分連結為聯盟連結,我們可能獲得佣金;優惠以商戶官方為準。</small>")
    return f"<h3>{head}</h3><ul>{''.join(_deal_li(d) for d in deals)}</ul>{foot}", unsub

async def email_once(client):
    if not (on("email_enabled") and get_secret("resend_api_key")): return
    t = now(); today = hk_today(); hk_hour = dt.datetime.now(HKT).hour
    with db() as c:
        subs = c.execute("SELECT * FROM email_subs WHERE confirmed=1").fetchall()
        fresh = c.execute("SELECT * FROM deals WHERE silent=0 AND created_at>? ORDER BY created_at", (t - 6 * 3600,)).fetchall()
        done = {(r["deal_id"], r["email"]) for r in c.execute("SELECT deal_id,email FROM email_sent WHERE ts>?", (t - 12 * 3600,))}
        day_deals = c.execute("SELECT * FROM deals WHERE silent=0 AND created_at>? ORDER BY created_at DESC LIMIT 15", (t - 86400,)).fetchall()
    for s in subs:
        vip = (s["vip_until"] or 0) > t
        if vip:   # VIP 即時
            for d in fresh:
                if (d["id"], s["email"]) in done: continue
                body, unsub = wrap([d], s["token"], "🔥 新優惠")
                if await send_email(client, s["email"], d["title"], body, unsub):
                    with db() as c: c.execute("INSERT OR IGNORE INTO email_sent VALUES(?,?,?)", (d["id"], s["email"], t))
        if hk_hour >= int(float(get_setting("digest_hour") or 9)) and s["last_digest"] != today:   # 每日 digest
            ds = [d for d in day_deals if not locked(d["created_at"])]
            if ds:
                body, unsub = wrap(ds, s["token"], "今日著數精選")
                await send_email(client, s["email"], f"著數飛|{today} 今日優惠精選", body, unsub)
            with db() as c: c.execute("UPDATE email_subs SET last_digest=? WHERE email=?", (today, s["email"]))

async def email_loop():
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try: await email_once(client)
            except Exception: log.exception("email loop failed")
            await asyncio.sleep(60)
