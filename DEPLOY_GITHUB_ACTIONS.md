# 🆓 GitHub Actions 部署指南 (永久免費, 取代 Railway)

## 為什麼搬過來

| 項目 | Railway | GitHub Actions |
|------|---------|----------------|
| 費用 | 試用後 $5/月起 | **公開 repo 永久免費, 無限制** |
| 適合度 | 24h 常駐 (但你的任務一天只跑幾次, 浪費) | **就是為定時任務設計的** |
| 設定 | Variables + Procfile | Secrets + workflow yml |
| 時區 | 要自己處理 | 支援 `timezone: Asia/Taipei` |

---

## 📤 Step 1 — 上傳檔案到 GitHub

把這些檔案放到你的 repo (跟現有的一起):

**新增的檔案**
```
run_once.py                        ← 執行一次就結束的入口 (取代 scheduler.py)
.github/workflows/disposal.yml     ← 排程設定 (cron)
.github/workflows/keepalive.yml    ← 防止排程被自動停用
results/.gitkeep                   ← 佔位 (讓 results 資料夾存在)
results/charts/.gitkeep            ← 佔位
```

**原本就有的 (確認都是最新版)**
```
core_stock.py  notify_telegram.py  notify_line.py
scan_tasks.py  github_store.py     requirements.txt
```

**可以刪掉的 (Railway 專用, GitHub Actions 用不到)**
```
scheduler.py   Procfile   railway.json   nixpacks.toml   runtime.txt
```
(留著也不影響, 只是用不到)

> ⚠️ `.github/workflows/` 是資料夾結構, GitHub 網頁上傳時檔名要打
> `.github/workflows/disposal.yml` (含斜線, GitHub 會自動建資料夾)

---

## 🔐 Step 2 — 設定 Secrets

repo 頁面 → **Settings** → 左側 **Secrets and variables** → **Actions**
→ **New repository secret**, 逐一加入:

| Secret 名稱 | 值 |
|------------|-----|
| `TELEGRAM_BOT_TOKEN` | 你的 Telegram bot token |
| `TELEGRAM_CHAT_ID` | 你的 Telegram chat id (數字) |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE 的 token (沒用 LINE 可跳過) |
| `LINE_TO` | 你的 LINE User ID (U 開頭) |

> 💡 **不用設 `GITHUB_TOKEN`!** GitHub Actions 會自動提供, workflow 裡已經接好了。

### (選用) 設定 Variables

同一頁切到 **Variables** 分頁 → **New repository variable**:

| Variable | 值 | 說明 |
|----------|-----|------|
| `CHART_TARGETS` | `telegram,line` | 誰要附 K 線圖 |
| | `test` | 🧪 測試模式: 只推 Telegram, LINE 靜音 |
| | `telegram` | 只有 Telegram 附圖 (省 LINE 額度) |
| | `none` | 都不附圖 |

不設定的話預設 `telegram,line` (兩個都附圖)。

---

## ▶️ Step 3 — 手動測試

1. repo → **Actions** 分頁
2. 左側點 **處置股推播**
3. 右邊 **Run workflow** 按鈕 → 確認 `force` 打勾 → **Run workflow**
4. 等 2~3 分鐘, 點進去看 log
5. 檢查 Telegram / LINE 有沒有收到

**首次執行可能要等一下** (要裝 Python 套件和中文字型)。

---

## ⏰ 排程時間 (全部台北時間)

| 時間 | 做什麼 |
|------|--------|
| 平日 **18:05** | 收盤後推最新處置股 (證交所公告已出) |
| 平日 **20:05** | 若 18:05 沒抓到資料 → 重試 |
| 平日 **22:05** | 再重試 |
| 隔天 **00:05** | 最後嘗試, 還是沒有就發「未抓到資料」通知 |
| 平日 **08:00** | 盤前提醒 |

**智慧防重複**: 每次執行會先檢查「今天是不是已經推過了」, 推過就跳過。
所以 18:05 成功推播後, 20:05/22:05/00:05 都會自動跳過, 不會重複洗你。

---

## ⚠️ 兩個重要提醒

### 1. 排程有延遲是正常的
GitHub Actions 的 cron **可能延遲 5~30 分鐘** (免費資源排隊)。
所以 18:05 的任務可能 18:20 才跑 — 這不影響功能 (處置股資料不會變)。

### 2. 60 天不活動會被停用
GitHub 規定: 公開 repo 連續 60 天沒有 commit, 排程會被自動停用。
→ **`keepalive.yml` 已經幫你處理了** (每月自動 commit 一次)。

---

## 🔍 除錯

- **沒收到推播** → Actions 分頁看該次執行的 log, 找紅色錯誤
- **權限錯誤 (403)** → repo Settings → Actions → General
  → 下方 **Workflow permissions** 選 **Read and write permissions** → Save
- **排程沒觸發** → 確認 workflow 檔案在**預設分支** (main) 上; 排程只從預設分支觸發

---

## 💰 費用

**$0**。公開 repo 的 GitHub Actions 完全免費、沒有分鐘數限制。
(私有 repo 每月 2000 分鐘免費, 你這個任務一個月大約用 100 分鐘, 也夠)
