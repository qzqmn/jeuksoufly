import os, re

def env(key, default=""):
    """讀環境變數;容錯 .env 行內註解(如 `KEY=   # 說明` 被當成值)。"""
    v = os.getenv(key)
    if v is None: return default
    v = v.strip()
    if v.startswith("#"): return default
    v = re.split(r"\s+#", v)[0].strip()
    return v if v != "" else default

def _int(k, d): 
    try: return int(env(k, d))
    except ValueError: return d
def _float(k, d):
    try: return float(env(k, d))
    except ValueError: return d

BASE_URL = env("BASE_URL", "http://localhost:8000").rstrip("/")
DB_PATH = env("DB_PATH", "/data/jeuksoufly.db")
DATA_DIR = os.path.dirname(DB_PATH) or "."
ADMIN_USER = env("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_CHAT_IDS = {int(x) for x in env("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = env("TELEGRAM_BOT_USERNAME", "").lstrip("@")
if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", BOT_USERNAME): BOT_USERNAME = ""
TP_TOKEN = env("TP_TOKEN", "")
TP_MARKER = env("TP_MARKER", "")
ROUTES = [tuple(r.strip().upper().split("-")) for r in env("ROUTES", "HKG-TYO,HKG-OSA,HKG-BKK,HKG-TPE,HKG-SEL,HKG-SIN").split(",") if "-" in r]
MONTHS_AHEAD = _int("MONTHS_AHEAD", 4)
FREE_DELAY = _int("FREE_DELAY_MIN", 30) * 60
POLL_SECONDS = _int("POLL_MINUTES", 30) * 60
FREE_ALERT_LIMIT = _int("FREE_ALERT_LIMIT", 2)
DEAL_DROP_RATIO = _float("DEAL_DROP_RATIO", 0.85)

CITY = {
    "HKG": "香港", "TYO": "東京", "OSA": "大阪", "NGO": "名古屋", "FUK": "福岡", "CTS": "札幌", "OKA": "沖繩",
    "BKK": "曼谷", "HKT": "布吉", "CNX": "清邁", "TPE": "台北", "KHH": "高雄", "SEL": "首爾", "PUS": "釜山",
    "SIN": "新加坡", "KUL": "吉隆坡", "SGN": "胡志明市", "HAN": "河內", "DAD": "峴港", "MNL": "馬尼拉",
    "CEB": "宿霧", "DPS": "峇里", "LON": "倫敦", "SYD": "悉尼", "MEL": "墨爾本", "YVR": "溫哥華", "VAN": "溫哥華",
    "NKG": "南京", "CTU": "成都", "SYX": "三亞", "HAK": "海口", "HGH": "杭州", "SHA": "上海", "PVG": "上海", "PEK": "北京", "PKX": "北京",
    "OSA": "大阪", "KIX": "大阪", "NRT": "東京", "HND": "東京", "ICN": "首爾", "CJU": "濟州", "RMQ": "台中", "HUN": "花蓮",
}

# ---- 付款 / Email ----
STRIPE_SECRET = env("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY = env("RESEND_API_KEY", "")
MAIL_FROM = env("MAIL_FROM", "著數飛 <deals@example.com>")
DIGEST_HOUR = _int("DIGEST_HOUR_HKT", 9)

# 後台可改的開關/參數(存於 settings 表,此處為預設值)
SETTING_DEFAULTS = {
    "email_enabled": "0",          # 開 = 接受 Email 訂閱並發送 digest / VIP 即時 Email
    "stripe_enabled": "0",         # 開 = VIP 頁顯示信用卡付款 (Stripe)
    "manual_pay_enabled": "0",     # 開 = VIP 頁顯示 FPS/PayMe 手動付款登記
    "price_monthly": "38",
    "price_yearly": "380",
    "free_delay_min": str(FREE_DELAY // 60),
    "manual_pay_text": "請以 FPS(轉數快)/PayMe 付款至 ○○○○○○○○,備註你的 Telegram chat_id 或 Email,付款後提交下方表格,管理員會盡快為你開通。",
    "site_notice": "",
    "deal_drop_ratio": str(DEAL_DROP_RATIO),   # 低於中位數幾多倍才算降價
    "min_history": "8",                        # 至少幾多個歷史價格點先開始判斷
    "keywords": "機票,著數,優惠,promo,code,$,折,減,回贈,快閃",
    "digest_hour": str(DIGEST_HOUR),
    "operator_name": "(請填寫營運者名稱/公司名稱)",
    "contact_email": "contact@example.com",
    "ai_model": "claude-haiku-5-5",
    "ai_auto_publish": "0",                    # 總開關:1 = 允許「自動發佈」模式的監察項目直接發佈
    "ai_min_confidence": "0.75",
    "ai_daily_cap": "20",                      # 每日最多自動發佈幾多條
    "ai_origins": "HKG",                       # 只收這些出發地的優惠(逗號分隔)
    "legal_draft": "1",                        # 1 = 法律頁顯示「草稿」橫額;律師審閱後在後台關閉
}
