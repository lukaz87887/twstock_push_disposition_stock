# -*- coding: utf-8 -*-
"""
scheduler.py — Railway 上 24h 常駐的排程器

工作:
  • 早上 08:00 (盤前): 掃處置股月線 → 存 GitHub → Telegram 推播
  • 晚上 21:00 (盤後): 掃全市場飆股 → 存 GitHub → Telegram 推播
  • 只在平日 (週一~週五) 執行

環境變數 (Railway Variables 設定):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   ← Telegram 推播 (選填)
  LINE_CHANNEL_ACCESS_TOKEN, LINE_TO     ← LINE 推播 (選填, 見 DEPLOY_LINE.md)
  GITHUB_TOKEN, GITHUB_REPO              ← 結果+K線圖存回 GitHub (必填)
  PUSH_CHARTS      (選填, 預設 1)          ← 是否附 K 線圖
  MORNING_HHMM     (選填, 預設 08:00)      ← 早上提醒時段
  EVENING_HHMM     (選填, 預設 18:00)      ← 傍晚收盤後最新 (證交所公告已出)
  RUN_ON_START     (選填, 預設 0)          ← 啟動就先跑一次 (測試用)
  TZ=Asia/Taipei                          ← 時區 (重要!)

處置股推播時段 (兩者都完整版含K線):
  • 傍晚 18:00 收盤後: 證交所公告已出, 抓當日最新處置名單
  • 早上 08:00 提醒:   開盤前再提醒今天哪些股票在處置中

本地測試:
  RUN_ON_START=1 python scheduler.py
"""
import os
import time
import traceback

# ★★★ 關鍵: 在 import datetime 之前強制設定時區為台北 ★★★
# 不依賴 Railway 的 TZ 變數是否正確套用, 直接在程式碼裡鎖死
os.environ["TZ"] = os.environ.get("TZ", "Asia/Taipei")
try:
    time.tzset()   # Linux/Unix 生效 (Railway 是 Linux)
except AttributeError:
    pass           # Windows 沒有 tzset, 本地測試時忽略

from datetime import datetime

import schedule

from scan_tasks import run_momentum_scan, run_disposal_scan, save_result
from github_store import push_json, push_bytes
from core_stock import is_market_open_today, is_fixed_holiday
from notify_telegram import (push_momentum, push_disposal, send_message,
                             make_kline_png)
import notify_line


def _env(key, default=""):
    return os.environ.get(key, default)


def _log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          flush=True)


def _is_weekday():
    return datetime.now().weekday() < 5


# ==================================================================
#   處置股推播 (含開盤判斷 + 重試機制)
# ==================================================================
# 記錄「哪一天已成功推播過」, 避免重試時重複推
_last_pushed_date = {"disposal": None}


def _notify_all(text: str):
    """同時發 Telegram + LINE 純文字通知 (test 模式只發 Telegram)"""
    if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
        try:
            send_message(text)
        except Exception:
            pass
    if _is_test_mode():
        return   # 🧪 測試模式: LINE 靜音
    if _env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"):
        try:
            notify_line.send_text(text)
        except Exception:
            pass


def try_disposal_push(slot_label="", is_last_attempt=False):
    """
    嘗試推播處置股, 但只在「證交所今天資料已就緒」時才推。

    slot_label: 這次是哪個時段觸發的 (18:00/20:00...) 用於 log
    is_last_attempt: 是否為最後一次嘗試 (00:00), 若仍失敗會發「未抓到」通知
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 今天已經成功推過 → 跳過 (避免重試時重複)
    if _last_pushed_date["disposal"] == today_str:
        _log(f"[{slot_label}] 今日已推播過, 跳過")
        return

    # 固定國定假日 → 完全不推 (連檢查都省)
    if is_fixed_holiday():
        _log(f"[{slot_label}] 今日為國定假日, 不推播")
        _last_pushed_date["disposal"] = today_str  # 標記今天不用再試
        return

    # 檢查證交所今天有沒有開盤/更新
    ready, reason = is_market_open_today()
    _log(f"[{slot_label}] 開盤檢查: {reason}")

    if not ready:
        if is_last_attempt:
            # 最後一次仍沒資料 → 判斷是假日還是異常
            # 週末/明確假日不發, 其他 (可能颱風假或證交所異常) 發通知
            wd = datetime.now().weekday()
            if wd >= 5:
                _log(f"[{slot_label}] 週末, 不發通知")
            else:
                _notify_all(f"📭 台股處置股提醒 ({today_str})\n"
                            f"今日到 00:00 仍未抓到證交所行情資料。\n"
                            f"可能原因: 颱風假/臨時休市/證交所延遲。\n"
                            f"(系統運作正常, 僅告知)")
                _log(f"[{slot_label}] 已發送「未抓到資料」通知")
            _last_pushed_date["disposal"] = today_str
        else:
            _log(f"[{slot_label}] 今日資料未就緒, 等待下一時段重試")
        return

    # 資料就緒 → 正式推播
    _log(f"[{slot_label}] ✅ 資料就緒, 開始推播")
    job_disposal(force=True, label=f"處置股({slot_label})")
    _last_pushed_date["disposal"] = today_str


def _gen_and_upload_charts(result: dict) -> dict:
    """產生「一張合併大圖」(接近月線的個股) 並 push 到 GitHub, 回傳 {"__combined__": raw_url}"""
    from notify_telegram import make_combined_kline_png
    near = [r for r in result.get("items", []) if r["abs_diff_pct"] <= 5]
    if not near:
        return {}
    png = make_combined_kline_png(near, data_date=result.get("data_date", ""))
    if not png:
        return {}
    # 檔名帶日期, 避免 LINE 圖片快取到舊圖
    date_tag = (result.get("data_date") or "").replace("-", "")
    path = f"results/charts/disposal_combined_{date_tag}.png"
    ok, url = push_bytes(path, png, commit_msg=f"combined chart {date_tag}")
    return {"__combined__": url} if ok else {}


def _chart_targets() -> set:
    """
    CHART_TARGETS 環境變數: 哪些管道要附 K 線圖。
      CHART_TARGETS=test            → 🧪 測試模式: 只推 Telegram (含圖), LINE 完全不推
      CHART_TARGETS=telegram,line   → 兩個都附圖
      CHART_TARGETS=telegram        → 只有 Telegram 附圖 (LINE 仍推文字)
      CHART_TARGETS=line            → 只有 LINE 附圖
      CHART_TARGETS=none (或留空)   → 都不附圖 (只推文字)
    未設定時預設 telegram,line (兩個都附)。
    """
    raw = _env("CHART_TARGETS", "telegram,line").strip().lower()
    if raw in ("", "none", "off", "0"):
        return set()
    if raw == "test":
        return {"telegram"}   # test 模式只有 Telegram 附圖
    return {t.strip() for t in raw.split(",") if t.strip()}


def _is_test_mode() -> bool:
    """🧪 測試模式: CHART_TARGETS=test → 只推 Telegram, LINE 完全靜音
    (方便測試時不浪費 LINE 免費額度, 也不會一直洗 LINE)"""
    return _env("CHART_TARGETS", "").strip().lower() == "test"


def job_disposal(force=False, label="處置股"):
    if not force and not _is_weekday():
        _log(f"週末, 跳過{label}")
        return
    _log(f"▶ 開始{label}掃描...")
    try:
        prev_codes = _load_prev_disposal_codes()
        result = run_disposal_scan(days_back=30, only_active=True,
                                   prev_codes=prev_codes)
        save_result("disposal", result)
        ok, msg = push_json("results/disposal.json", result,
                            commit_msg=f"disposal {result.get('data_date')}")
        _log(f"  GitHub 存檔: {'OK' if ok else msg}")
        _log(f"  本日新增 {len(result.get('added_today',[]))} 檔, "
             f"出關 {len(result.get('removed_today',[]))} 檔")

        targets = _chart_targets()
        test_mode = _is_test_mode()
        tg_charts = "telegram" in targets
        line_charts = "line" in targets

        if test_mode:
            _log("  🧪 測試模式 (CHART_TARGETS=test): 只推 Telegram(含圖), LINE 靜音")
        else:
            _log(f"  K線圖設定 CHART_TARGETS: "
                 f"Telegram={'ON' if tg_charts else 'off'}, "
                 f"LINE={'ON' if line_charts else 'off'}")

        # 只有 LINE 要圖時才需上傳 GitHub (Telegram 直接傳位元組)
        chart_urls = {}
        if line_charts and not test_mode:
            chart_urls = _gen_and_upload_charts(result)
            _log(f"  已產生並上傳 {len(chart_urls)} 張 K 線圖 (供 LINE 使用)")

        if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
            try:
                push_disposal(result, with_charts=tg_charts)
                _log(f"  ✔ Telegram 推播完成 (附圖={tg_charts})")
            except Exception as e:
                _log(f"  ✘ Telegram 推播失敗: {e}")

        # 測試模式 → LINE 完全不推
        if test_mode:
            _log("  ⏭ LINE 已跳過 (測試模式)")
        elif _env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"):
            try:
                notify_line.push_disposal_line(
                    result, chart_urls=chart_urls if line_charts else {})
                _log(f"  ✔ LINE 推播完成 (附圖={line_charts})")
            except Exception as e:
                _log(f"  ✘ LINE 推播失敗: {e}")

        _log(f"✔ {label}完成, {result.get('scanned',0)} 檔")
    except Exception as e:
        _log(f"✘ {label}失敗: {e}\n{traceback.format_exc()}")
        _notify_all(f"❌ {label}排程失敗: {e}")


# 早上時段 (盤前提醒): 直接推, 不用等當日資料 (推的是昨天收盤後已定的名單)
def job_morning_disposal(force=False):
    if not force and not _is_weekday():
        _log("週末, 跳過盤前提醒")
        return
    if is_fixed_holiday():
        _log("國定假日, 跳過盤前提醒")
        return
    job_disposal(force=force, label="盤前提醒")


def _load_prev_disposal_codes():
    """讀 GitHub 上現有的 disposal.json, 回傳昨天的 {代碼: 名稱} 字典
    (帶名稱才能在「本日出關」顯示中文股名)"""
    import requests as _rq
    repo = _env("GITHUB_REPO", "")
    branch = _env("GITHUB_BRANCH", "main")
    if not repo:
        return None
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/results/disposal.json"
    try:
        r = _rq.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", [])
            if items:
                return {it["code"]: it.get("name", "")
                        for it in items if it.get("code")}
            # 舊格式相容: 只有 all_codes 沒有名稱
            if "all_codes" in data:
                return {c: "" for c in data["all_codes"]}
    except Exception as e:
        _log(f"  (讀取昨日清單失敗, 首次執行屬正常: {e})")
    return None


# ==================================================================
#   晚上: 飆股
# ==================================================================
def job_evening_momentum(force=False):
    if not force and not _is_weekday():
        _log("週末, 跳過盤後飆股")
        return
    level = _env("MOMENTUM_LEVEL", "all")
    preset = _env("MOMENTUM_PRESET", "standard")
    _log(f"▶ 開始盤後飆股掃描 (level={level}, preset={preset})...")
    try:
        def _pcb(ratio, text):
            if int(ratio * 100) % 20 == 0:
                _log(f"  {int(ratio*100)}% {text}")
        result = run_momentum_scan(level=level, preset=preset,
                                   progress_cb=_pcb)
        save_result(f"momentum_{level}", result)
        ok, msg = push_json(f"results/momentum_{level}.json", result,
                            commit_msg=f"momentum {result.get('data_date')}")
        _log(f"  GitHub 存檔: {'OK' if ok else msg}")
        with_charts = _env("PUSH_CHARTS", "1") == "1"
        push_momentum(result, with_charts=with_charts)
        _log(f"✔ 盤後飆股完成, 掃 {result.get('scanned',0)} 檔, "
             f"符合 {len(result.get('items',[]))} 檔")
    except Exception as e:
        _log(f"✘ 盤後飆股失敗: {e}\n{traceback.format_exc()}")
        send_message(f"❌ 盤後飆股排程失敗: {e}")


def main():
    morning = _env("MORNING_HHMM", "08:00")     # 早上提醒

    tg_on = bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))
    line_on = bool(_env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"))
    test_mode = _is_test_mode()

    _log("=" * 50)
    _log("台股處置股排程器啟動")
    _log(f"  🌆 收盤後推最新 (含重試): 18:00 → 20:00 → 22:00 → 00:00")
    _log(f"     每時段先檢查證交所今日資料是否就緒, 就緒才推, 只推一次")
    _log(f"     到 00:00 仍無資料 → 發「未抓到」通知 (假日/颱風除外)")
    _log(f"  🌅 早上提醒: 每個平日 {morning}")
    _log(f"  推播管道: Telegram={'ON' if tg_on else 'off'}  "
         f"LINE={'靜音(測試模式)' if test_mode else ('ON' if line_on else 'off')}")
    if test_mode:
        _log("  🧪 CHART_TARGETS=test → 只推 Telegram(含圖), LINE 完全不推")
    _log(f"  時區 TZ={_env('TZ', '(未設定)')}")
    # ★ 時區驗證: 印出本地時間 + UTC 時間對照, 一眼確認有沒有生效
    from datetime import timezone
    now_local = datetime.now()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    offset_h = round((now_local - now_utc).total_seconds() / 3600)
    _log(f"  現在時間(本地): {now_local.strftime('%Y-%m-%d %H:%M:%S %A')}")
    _log(f"  現在時間(UTC) : {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    _log(f"  時差: UTC{offset_h:+d}  "
         f"{'✅ 台北時間正確 (UTC+8)' if offset_h == 8 else '⚠️ 不是 UTC+8! 排程時間會錯!'}")
    _log("=" * 50)

    # 傍晚重試序列: 18→20→22→00, 每時段檢查資料就緒才推 (已推過會自動跳過)
    schedule.every().day.at("18:00").do(
        lambda: try_disposal_push("18:00"))
    schedule.every().day.at("20:00").do(
        lambda: try_disposal_push("20:00"))
    schedule.every().day.at("22:00").do(
        lambda: try_disposal_push("22:00"))
    schedule.every().day.at("00:00").do(
        lambda: try_disposal_push("00:00", is_last_attempt=True))

    # 早上提醒 (推的是前一交易日收盤後已定的名單, 不需等當日資料)
    schedule.every().day.at(morning).do(job_morning_disposal)

    # 飆股推播目前關閉 (要恢復把下行取消註解)
    # schedule.every().day.at("21:00").do(job_evening_momentum)

    # 啟動即跑 (測試用) — 強制推一次, 略過開盤檢查
    if _env("RUN_ON_START", "0") == "1":
        _log("RUN_ON_START=1 → 立即強制執行一次 (略過開盤檢查)")
        job_disposal(force=True, label="測試處置股")

    _log("進入排程等待迴圈 (每 30 秒檢查一次)...")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            _log(f"排程迴圈錯誤: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
