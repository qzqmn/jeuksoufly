# 著數飛 (Jeuk Sou Fly)

香港人外遊/北上優惠與機票降價監控:Web + Telegram Bot,Docker 自行部署。

## 部署(鏡像由 GitHub Actions 構建)
1. 把專案推上 GitHub(`main` 分支),Actions 自動構建 amd64+arm64 鏡像到 `ghcr.io/<你的用戶名>/jeuksoufly:latest`。
   私有倉庫/私有套件:伺服器先 `docker login ghcr.io`(用有 read:packages 的 PAT)。
2. 伺服器:
```bash
cp .env.example .env        # 填 ADMIN_PASSWORD、TELEGRAM_BOT_TOKEN、TP_TOKEN 等
mkdir -p data
cp data/sources.example.json data/sources.json      # 優惠來源 (RSS / CSS 爬蟲)
cp data/affiliate.example.json data/affiliate.json  # 填入你的 Klook / Trip.com 聯盟 deeplink 模板
docker compose pull && docker compose up -d      # 更新同一條指令
```
本機直接構建:`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`。

### Cloudflare Tunnel(可選,在你自己的 CF 帳戶操作)
Zero Trust → Networks → Tunnels → Create → 選 Cloudflared → 複製 token 填 `.env` 的 `TUNNEL_TOKEN` →
Public Hostname 設 `jeuksoufly.你的域名` → Service `http://jeuksoufly:8000`;然後 `docker compose --profile tunnel up -d`。
前面用 Caddy / Nginx / Cloudflare Tunnel 反代 8000 端口,並把 `BASE_URL` 設成正式網址。
後台:`/admin`(HTTP Basic,帳密見 .env)。

## 票價來源(後台「數據來源」可隨時增減、啟停、改間隔、一鍵測試航線覆蓋)
- **Travelpayouts / Aviasales**:免費,但數據是用戶搜尋快取(非即時),香港航線可能稀疏 → 先用「測試」看覆蓋。
- **Ignav**:即時搜尋價,HK 市場回傳 HKD;首 1,000 次請求免費,之後約 US$2/1000 次(以官方定價為準)。費用 ≈ 航線數 × offsets 數 × 每日輪數 × 30。預設每 6 小時一輪、4 個出發日。
- **SerpApi Google Flights**:每月 250 次免費搜尋,付費方案約從 US$50/月(5,000 次)起(第三方文章所載,以官方定價頁為準)。回應含 Google 的「價格偏低/一般/偏高」與典型價範圍,可補足歷史數據不足的冷啟動問題。
- Amadeus Self-Service 已於 2026-07-17 停止服務,不要再用。
- 各來源的降價比較**分開計算**(不混合不同來源的價格)。

## 航空公司促銷頁 AI 監察(後台「✈️ 促銷頁監察」)
定時抓官方促銷頁 → 內容有變才呼叫 Claude 抽取 → 程式驗證 → 自動發佈或進收件匣。
- 驗證規則:價格必須在頁面原文出現(防幻覺)、貨幣須 HKD、價格 30–30,000、出發地在名單內(預設 HKG)、未過期、信心 ≥ 門檻、每日自動發佈上限。
- 標題由程式組成;優惠截止日自動成為下架時間;首次抓取只入庫不推送。
- 預設進收件匣;需要全自動時:項目選「自動發佈」並開啟總開關。
- 遵守 robots.txt;純 JavaScript 渲染的頁面會提示讀不到(日後可加瀏覽器渲染)。
- 法律:航空公司條款(例如 HK Express 第 5.3 條)限制為商業目的複製內容或建立資料庫,正式自動發佈前請取得書面同意或查詢夥伴計劃。

## 優惠收件匣
RSS/爬蟲抓到的項目預設進後台「收件匣」,由你用自己的文字改寫並改連到商戶官方頁後才發佈(來源 config 加 `"auto": true` 才會自動發佈,不建議)。

## 法律頁與取消訂閱
`/privacy`、`/terms` 為範本草稿(頁面會顯示草稿警示),須由專業人士審閱,再到後台填營運者名稱/聯絡電郵並勾選「已審閱」。信用卡訂戶可在 `/vip/manage`(Email 魔術連結)或 Telegram `/manage` 進入 Stripe Customer Portal 取消;須先在 Stripe 後台啟用 Customer portal。

## 信用卡資料
沒有官方 API。後台「信用卡」:加入銀行條款頁網址 → 系統每 6 小時檢查內容雜湊,變更時在數據頁亮警示 → 你核對後改 JSON(含驗證)。純 JS 渲染的頁面無法監察(會顯示錯誤)。

## 後台 `/admin`
- 📊 數據:瀏覽/點擊/點擊率、14 日趨勢、TG/Email 訂閱與 VIP、熱門優惠、按商戶點擊、聯盟與 VIP 收入、各數據來源健康度。
- 🔥 優惠管理:手動新增/刪除。
- 🛰 數據來源 / 💳 信用卡 / 🔑 整合設定(Resend、Travelpayouts、Ignav、Stripe 密鑰,無須重啟,可發測試 Email)。
- ⚙️ 開關・付款:Email 開關、Stripe 開關、FPS/PayMe 手動登記開關、月費/年費、免費延遲分鐘、全站公告、核實手動付款、手動開通 VIP、登記聯盟收入。

## Stripe / Email 設定
- Stripe:填 `STRIPE_SECRET_KEY`;Webhook 指向 `BASE_URL/stripe/webhook`,事件選 `checkout.session.completed` 與 `invoice.paid`,把簽名密鑰填 `STRIPE_WEBHOOK_SECRET`;再到後台開啟開關。取消訂閱目前由 Stripe 後台處理。
- Email:Resend 驗證寄件域名後填 `RESEND_API_KEY`、`MAIL_FROM`;雙重確認 (double opt-in)、一鍵取消訂閱;每日 digest 於 `DIGEST_HOUR_HKT` 發送,VIP 即時。

## 功能
- **機票降價**:Travelpayouts (Aviasales) API 按 `ROUTES` 定時抓取,與近 30 日中位數比較,低於 `DEAL_DROP_RATIO` 自動生成優惠。
- **優惠來源**:`data/sources.json` 可配 RSS/Atom 與 CSS selector 爬蟲;新來源首次只放最新 5 條且不推送。後台可人手加快閃優惠。
- **Telegram Bot**:`/deals /follow /alert /alerts /delalert /me /vip`;管理員 `/grantvip <chat_id> <日數>`。
- **免費/VIP**:免費延遲 `FREE_DELAY_MIN` 分鐘推送(網站亦會鎖住新優惠),VIP 即時;價格提醒免費有數量上限。目前 VIP 由後台手動開通,收費未接駁。
- **回贈計算機**:`/cards`,內置數據只是示例,請複製 `app/cards_default.json` 到 `data/cards.json` 改成真實條款。
- **Affiliate**:所有出站連結經 `/go/<id>`,記錄點擊並按 `affiliate.json` 套用聯盟模板。

## IP 被封 / 反爬
Docker 部署在自己的 VPS/NAS,可設 `HTTPS_PROXY`;請遵守目標網站條款與 robots.txt,優先用官方 API/RSS。

## 上線前必做
1. 申請 Travelpayouts、Klook、Trip.com 及各銀行 referral,填入 `.env` / `affiliate.json`。
2. 改 `cards.json` 為真實回贈資料並定期核對。
3. 查 `著數飛` 域名及商標是否可用。
4. 保留頁尾聯盟披露,避免誤導性字眼。
