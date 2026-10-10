import asyncio, html, logging
import httpx
from . import config as C
from .db import db
from .store import now, is_vip, locked, free_delay, get_routes

log = logging.getLogger("bot")
API = f"https://api.telegram.org/bot{C.BOT_TOKEN}"

def help_text():
    return (
        "<b>著數飛 Bot</b> ✈️\n"
        "/deals — 最新優惠\n"
        "/follow TYO OSA — 只收指定目的地(城市代碼),/follow all 收全部\n"
        "/alert HKG TYO 1500 — 價格低於 HK$1,500 時提醒\n"
        "/alerts — 我的提醒 · /delalert 編號 — 刪除\n"
        "/me — 我的狀態 · /vip — VIP 即時推送 · /manage — 管理/取消訂閱\n\n"
        f"可追蹤航線:{', '.join(f'{o}-{d}' for o, d in get_routes())}"
    )

async def send(client, chat_id, text):
    try:
        r = await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                                         "disable_web_page_preview": True})
        if r.status_code == 403:
            with db() as c: c.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
        return r.status_code == 200
    except Exception as e:
        log.warning("send failed: %s", e); return False

def fmt_deal(d):
    icon = {"flight": "✈️", "cn": "🚄", "card": "💳"}.get(d["kind"], "🔥")
    s = f"{icon} <b>{html.escape(d['title'])}</b>"
    if d["summary"]: s += f"\n{html.escape(d['summary'])}"
    if d["code"]: s += f"\n優惠碼:<code>{html.escape(d['code'])}</code>"
    s += f"\n👉 {C.BASE_URL}/go/{d['id']}"
    return s

def _sub(c, chat_id, username=""):
    row = c.execute("SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        c.execute("INSERT INTO subscribers(chat_id,username,created_at) VALUES(?,?,?)", (chat_id, username, now()))
        row = c.execute("SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
    return row

async def handle(client, upd):
    m = upd.get("message") or {}
    text = (m.get("text") or "").strip()
    chat_id = (m.get("chat") or {}).get("id")
    if not text or chat_id is None: return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower(); args = parts[1:]
    with db() as c:
        sub = _sub(c, chat_id, (m.get("from") or {}).get("username", ""))
        vip = is_vip(sub)

        if cmd in ("/start", "/help"):
            return await send(client, chat_id, help_text())

        if cmd == "/deals":
            rows = c.execute("SELECT * FROM deals ORDER BY created_at DESC LIMIT 12").fetchall()
            rows = [d for d in rows if vip or not locked(d["created_at"])][:5]
            return await send(client, chat_id, "\n\n".join(fmt_deal(d) for d in rows) or "暫時未有優惠,稍後再試 🙏")

        if cmd == "/follow":
            if not args or args[0].lower() == "all":
                c.execute("UPDATE subscribers SET dests='' WHERE chat_id=?", (chat_id,))
                return await send(client, chat_id, "✅ 已設定為接收全部優惠")
            codes = [a.upper() for a in args if len(a) == 3 and a.isalpha()]
            if not codes: return await send(client, chat_id, "請用城市代碼,例如 /follow TYO OSA")
            c.execute("UPDATE subscribers SET dests=? WHERE chat_id=?", (",".join(codes), chat_id))
            return await send(client, chat_id, f"✅ 只推送目的地:{', '.join(C.CITY.get(x, x) for x in codes)}(無目的地的優惠仍會推送)")

        if cmd == "/alert":
            if len(args) != 3 or not args[2].replace(".", "", 1).isdigit():
                return await send(client, chat_id, "格式:/alert HKG TYO 1500")
            o, d, p = args[0].upper(), args[1].upper(), float(args[2])
            if (o, d) not in get_routes():
                return await send(client, chat_id, "此航線未有追蹤。可追蹤:" + ", ".join(f"{a}-{b}" for a, b in get_routes()))
            n = c.execute("SELECT COUNT(*) n FROM alerts WHERE chat_id=?", (chat_id,)).fetchone()["n"]
            if not vip and n >= C.FREE_ALERT_LIMIT:
                return await send(client, chat_id, f"免費用戶最多 {C.FREE_ALERT_LIMIT} 個提醒,升級 VIP 不限數量:/vip")
            c.execute("INSERT INTO alerts(chat_id,origin,dest,max_price) VALUES(?,?,?,?)", (chat_id, o, d, p))
            return await send(client, chat_id, f"✅ 已設定:{C.CITY.get(o, o)}→{C.CITY.get(d, d)} 低於 HK${p:,.0f} 時通知")

        if cmd == "/alerts":
            rows = c.execute("SELECT * FROM alerts WHERE chat_id=?", (chat_id,)).fetchall()
            return await send(client, chat_id, "\n".join(f"#{r['id']} {r['origin']}→{r['dest']} ≤ HK${r['max_price']:,.0f}" for r in rows) or "未有提醒,用 /alert 新增")

        if cmd == "/delalert" and args and args[0].isdigit():
            c.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(args[0]), chat_id))
            return await send(client, chat_id, "🗑 已刪除")

        if cmd == "/me":
            import datetime as dt
            st = "VIP 至 " + dt.datetime.fromtimestamp(sub["vip_until"]).strftime("%Y-%m-%d") if vip else f"免費(推送延遲 {free_delay() // 60} 分鐘)"
            return await send(client, chat_id, f"身份:{st}\n目的地過濾:{sub['dests'] or '全部'}\nchat_id:<code>{chat_id}</code>")

        if cmd == "/manage":
            row = c.execute("SELECT customer_id FROM stripe_customers WHERE tg=? LIMIT 1", (chat_id,)).fetchone()
            if not row: return await send(client, chat_id, "找不到你的信用卡訂閱紀錄。若是 FPS/PayMe 付款(一次性),無需取消,到期自動失效。")
            try:
                from .payments import create_portal
                url = await create_portal(row["customer_id"])
                return await send(client, chat_id, f"🔧 管理/取消訂閱(連結短時間內有效):\n{url}")
            except Exception as e:
                log.warning("portal: %s", e); return await send(client, chat_id, "暫時無法開啟管理頁面,請稍後再試或聯絡我們。")

        if cmd == "/vip":
            return await send(client, chat_id, f"💎 VIP:優惠即時推送(免費用戶延遲 {free_delay() // 60} 分鐘)、不限價格提醒數量。\n升級連結:{C.BASE_URL}/vip?tg={chat_id}\n你的 chat_id:<code>{chat_id}</code>")

        if cmd == "/grantvip" and chat_id in C.ADMIN_CHAT_IDS and len(args) == 2 and args[1].isdigit():
            target = int(args[0]); _sub(c, target)
            base = max(now(), c.execute("SELECT vip_until FROM subscribers WHERE chat_id=?", (target,)).fetchone()["vip_until"] or 0)
            c.execute("UPDATE subscribers SET vip_until=? WHERE chat_id=?", (base + int(args[1]) * 86400, target))
            return await send(client, chat_id, f"✅ {target} VIP +{args[1]} 日")
    await send(client, chat_id, help_text())

async def poll_loop():
    if not C.BOT_TOKEN: return
    offset = 0
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            try:
                r = await client.get(f"{API}/getUpdates", params={"timeout": 50, "offset": offset})
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    try: await handle(client, u)
                    except Exception: log.exception("handle failed")
            except Exception as e:
                log.warning("getUpdates: %s", e); await asyncio.sleep(5)

async def push_loop():
    """每 30 秒:VIP 即時、免費延遲 FREE_DELAY 後推送;再檢查價格提醒"""
    if not C.BOT_TOKEN: return
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try: await push_once(client)
            except Exception: log.exception("push failed")
            await asyncio.sleep(30)

async def push_once(client):
    t = now()
    with db() as c:
        deals = c.execute("SELECT * FROM deals WHERE silent=0 AND created_at>? ORDER BY created_at", (t - 6 * 3600,)).fetchall()
        subs = c.execute("SELECT * FROM subscribers").fetchall()
        sent = {(r["deal_id"], r["chat_id"]) for r in c.execute("SELECT deal_id,chat_id FROM sent WHERE ts>?", (t - 12 * 3600,))}
    todo = []
    for d in deals:
        for s in subs:
            if (d["id"], s["chat_id"]) in sent: continue
            if not is_vip(s) and t - d["created_at"] < free_delay(): continue
            if s["dests"] and d["dest"] and d["dest"] not in s["dests"].split(","): continue
            todo.append((d, s))
    for d, s in todo:
        await send(client, s["chat_id"], fmt_deal(d))
        with db() as c: c.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?)", (d["id"], s["chat_id"], t))
        await asyncio.sleep(0.05)
    # 價格提醒
    with db() as c:
        alerts = c.execute("SELECT a.*, s.vip_until FROM alerts a JOIN subscribers s USING(chat_id)").fetchall()
    for a in alerts:
        delay = 0 if a["vip_until"] > t else free_delay()
        with db() as c:
            row = c.execute("SELECT * FROM price_history WHERE origin=? AND dest=? AND ts>? AND ts<=? ORDER BY price LIMIT 1",
                            (a["origin"], a["dest"], t - 86400, t - delay)).fetchone()
        if not row or row["price"] > a["max_price"]: continue
        if a["last_price"] and row["price"] >= a["last_price"] and t - a["last_at"] < 86400: continue
        link = f"{C.BASE_URL}/flights"
        await send(client, a["chat_id"], f"🔔 <b>價格提醒</b> {C.CITY.get(a['origin'], a['origin'])}→{C.CITY.get(a['dest'], a['dest'])}\n"
                                         f"HK${row['price']:,.0f}({row['depart_date']} 出發)低過你設定的 HK${a['max_price']:,.0f}\n👉 {link}")
        with db() as c: c.execute("UPDATE alerts SET last_price=?, last_at=? WHERE id=?", (row["price"], t, a["id"]))
