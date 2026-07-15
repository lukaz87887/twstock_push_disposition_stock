# -*- coding: utf-8 -*-
"""
run_once.py — GitHub Actions 專用入口 (執行一次就結束, 不常駐)

與 scheduler.py 的差異:
  scheduler.py : 24h 常駐, 自己用 schedule 套件等時間 (Railway 用)
  run_once.py  : 跑一次就結束, 排程交給 GitHub Actions 的 cron (免費!)

用法:
  python run_once.py disposal        # 處置股 (自動判斷資料是否就緒)
  python run_once.py disposal --force # 強制跑 (略過開盤檢查, 測試用)
  python run_once.py momentum        # 飆股 (目前預設關閉)

環境變數 (GitHub Secrets 設定):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    ← Telegram 推播
  LINE_CHANNEL_ACCESS_TOKEN, LINE_TO      ← LINE 推播 (選填)
  GITHUB_TOKEN, GITHUB_REPO               ← 結果存回 repo
  CHART_TARGETS                           ← 誰要附圖 (telegram,line / test / none)
  IS_LAST_ATTEMPT                         ← "1" 表示今日最後一次嘗試 (00:00 那班)
"""
import os
import sys
import time
import traceback

# ★ 時區: GitHub Actions runner 預設 UTC, 強制設台北
os.environ["TZ"] = os.environ.get("TZ", "Asia/Taipei")
try:
    time.tzset()
except AttributeError:
    pass

from datetime import datetime, timedelta

from scan_tasks import run_disposal_scan, run_momentum_scan, save_result
from github_store import push_json, push_bytes
from core_stock import is_market_open_today, is_fixed_holiday
from notify_telegram import (push_disposal, push_momentum, send_message,
                             make_combined_kline_png)
import notify_line


def _env(k, d=""):
    return os.environ.get(k, d)


def _log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _chart_targets() -> set:
    raw = _env("CHART_TARGETS", "telegram,line").strip().lower()
    if raw in ("", "none", "off", "0"):
        return set()
    if raw == "test":
        return {"telegram"}
    return {t.strip() for t in raw.split(",") if t.strip()}


def _is_test_mode() -> bool:
    return _env("CHART_TARGETS", "").strip().lower() == "test"


def _notify_all(text: str):
    """Telegram + LINE 純文字通知 (test 模式只發 Telegram)"""
    if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
        try:
            send_message(text)
        except Exception:
            pass
    if _is_test_mode():
        return
    if _env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"):
        try:
            notify_line.send_text(text)
        except Exception:
            pass


def _load_prev_disposal():
    """讀 repo 上現有的 disposal.json, 回傳 {代碼: 名稱}"""
    import requests
    repo = _env("GITHUB_REPO", "")
    branch = _env("GITHUB_BRANCH", "main")
    if not repo:
        return None
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/results/disposal.json"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", [])
            if items:
                return {it["code"]: it.get("name", "")
                        for it in items if it.get("code")}
            if "all_codes" in data:
                return {c: "" for c in data["all_codes"]}
    except Exception as e:
        _log(f"  (讀昨日清單失敗, 首次執行屬正常: {e})")
    return None


def _upload_combined_chart(result: dict) -> dict:
    """產生合併大圖並 push 到 repo, 回傳 {"__combined__": url}"""
    near = [r for r in result.get("items", []) if r["abs_diff_pct"] <= 5]
    if not near:
        return {}
    png = make_combined_kline_png(near, data_date=result.get("data_date", ""))
    if not png:
        return {}
    date_tag = (result.get("data_date") or "").replace("-", "")
    path = f"results/charts/disposal_combined_{date_tag}.png"
    ok, url = push_bytes(path, png, commit_msg=f"chart {date_tag}")
    return {"__combined__": url} if ok else {}


# ==================================================================
#   處置股
# ==================================================================
def run_disposal(force: bool = False):
    today_str = datetime.now().strftime("%Y-%m-%d")
    is_last = _env("IS_LAST_ATTEMPT", "0") == "1"

    _log("=" * 50)
    _log(f"處置股任務  (今天: {today_str})")
    _log(f"  強制模式: {force}   最後嘗試: {is_last}")
    _log(f"  時區檢查: 本地 {datetime.now().strftime('%H:%M')}")
    _log("=" * 50)

    if not force:
        pushed_date = _get_pushed_date()          # 已推播的資料日期 (字串或 None)
        pushed = _norm_date(pushed_date)

        if is_last:
            # ── 凌晨 00:05 那班: 此刻已跨到「隔天」, 它其實是「前一交易日」的收尾 ──
            #    所以日期判斷全部改用「昨天」, 而不是 datetime.now() 的今天,
            #    而且「不」呼叫 is_market_open_today() (它只會查隔天、必然落空 → 誤報來源)
            trading_day = datetime.now() - timedelta(days=1)
            trading_str = trading_day.strftime("%Y-%m-%d")

            # (1) 前一交易日已成功推播 → 靜默跳過 (這就是原本會誤報的情境)
            if pushed and pushed == _norm_date(trading_str):
                _log(f"前一交易日 ({trading_str}) 已成功推播, 凌晨班跳過")
                return
            # (2) fail-safe: 讀不到推播紀錄時, 寧可不發, 也不要誤報
            if pushed_date is None:
                _log("無法讀取推播紀錄, 為避免誤報, 凌晨班不發通知")
                return
            # (3) 前一交易日本就是週末/固定假日 → 本來就不該有資料, 靜默跳過
            if trading_day.weekday() >= 5 or is_fixed_holiday(trading_day):
                _log(f"前一交易日 ({trading_str}) 為假日, 凌晨班跳過")
                return
            # (4) 走到這 = 前一交易日整天都沒抓到 → 才是真異常, 發通知
            _notify_all(f"📭 台股處置股提醒 ({trading_str})\n"
                        f"該交易日到最後一班仍未抓到證交所行情資料。\n"
                        f"可能原因: 颱風假/臨時休市/證交所延遲。\n"
                        f"(系統運作正常, 僅告知)")
            _log(f"前一交易日 ({trading_str}) 未成功推播 → 已發送「未抓到資料」通知")
            return

        # ── 一般班次 (18:05 / 20:05 / 22:05): 以「今天」判斷 ──
        # 今天已推過就跳過 (擋掉同日多班重複推播)
        if pushed and pushed == _norm_date(today_str):
            _log(f"今日 ({today_str}) 已推播過, 跳過 (避免重複)")
            return
        # 週末不跑
        if datetime.now().weekday() >= 5:
            _log("週末, 跳過")
            return
        # 固定國定假日不跑
        if is_fixed_holiday():
            _log("國定假日, 跳過 (不推播)")
            return
        # 檢查證交所今天資料是否就緒
        ready, reason = is_market_open_today()
        _log(f"開盤檢查: {reason}")
        if not ready:
            _log("資料未就緒, 等下一班重試 (由 GitHub Actions cron 觸發)")
            return

    _log("▶ 開始掃描處置股...")
    prev_map = _load_prev_disposal()
    result = run_disposal_scan(days_back=30, only_active=True,
                               prev_codes=prev_map)
    save_result("disposal", result)

    ok, msg = push_json("results/disposal.json", result,
                        commit_msg=f"disposal {result.get('data_date')}")
    _log(f"  GitHub 存檔: {'OK' if ok else msg}")
    _log(f"  處置中 {result.get('scanned',0)} 檔, "
         f"新增 {len(result.get('added_today',[]))} 檔, "
         f"出關 {len(result.get('removed_today',[]))} 檔")

    targets = _chart_targets()
    test_mode = _is_test_mode()
    tg_charts = "telegram" in targets
    line_charts = "line" in targets
    if test_mode:
        _log("  🧪 測試模式: 只推 Telegram(含圖), LINE 靜音")

    chart_urls = {}
    if line_charts and not test_mode:
        chart_urls = _upload_combined_chart(result)
        _log(f"  合併圖已上傳: {'OK' if chart_urls else '失敗'}")

    if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
        try:
            push_disposal(result, with_charts=tg_charts)
            _log(f"  ✔ Telegram 完成 (附圖={tg_charts})")
        except Exception as e:
            _log(f"  ✘ Telegram 失敗: {e}")

    if test_mode:
        _log("  ⏭ LINE 已跳過 (測試模式)")
    elif _env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"):
        try:
            notify_line.push_disposal_line(
                result, chart_urls=chart_urls if line_charts else {})
            _log(f"  ✔ LINE 完成 (附圖={line_charts})")
        except Exception as e:
            _log(f"  ✘ LINE 失敗: {e}")

    _log(f"✔ 完成")


def _norm_date(s) -> str:
    """各種日期格式統一成 'YYYYMMDD' 再比較 (防未來 data_date 格式變動)。
    支援 2026-07-14 / 2026/07/14 / 20260714 / 民國 1140714 / 114/07/14。"""
    if not s:
        return ""
    digits = "".join(ch for ch in str(s) if ch.isdigit())
    if len(digits) == 8:            # 20260714 (西元)
        return digits
    if len(digits) == 7:            # 1140714 (民國年)
        return str(int(digits[:3]) + 1911) + digits[3:]
    return digits                   # 其他長度原樣 (保底)


def _get_pushed_date():
    """讀 repo 上 disposal.json 的 data_date (最後一次成功推播的資料日期)。
    回傳日期字串; 讀不到 (無 repo / 網路失敗 / 檔案不存在) 時回傳 None。
    注意: 呼叫端需自行 fail-safe, 不可把 None 當成「沒推過」而誤發通知。"""
    import requests
    repo = _env("GITHUB_REPO", "")
    branch = _env("GITHUB_BRANCH", "main")
    if not repo:
        return None
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/results/disposal.json"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json().get("data_date")
    except Exception:
        pass
    return None


# ==================================================================
#   飆股 (目前預設不啟用, 保留)
# ==================================================================
def run_momentum():
    level = _env("MOMENTUM_LEVEL", "all")
    preset = _env("MOMENTUM_PRESET", "standard")
    _log(f"▶ 飆股掃描 (level={level}, preset={preset})...")
    result = run_momentum_scan(level=level, preset=preset)
    save_result(f"momentum_{level}", result)
    push_json(f"results/momentum_{level}.json", result,
              commit_msg=f"momentum {result.get('data_date')}")
    if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
        push_momentum(result, with_charts=("telegram" in _chart_targets()))
    _log("✔ 飆股完成")


def main():
    args = sys.argv[1:]
    task = args[0] if args else "disposal"
    force = "--force" in args

    try:
        if task == "disposal":
            run_disposal(force=force)
        elif task == "momentum":
            run_momentum()
        else:
            _log(f"未知任務: {task} (可用: disposal / momentum)")
            sys.exit(1)
    except Exception as e:
        _log(f"✘ 任務失敗: {e}\n{traceback.format_exc()}")
        _notify_all(f"❌ 台股排程失敗 ({task}): {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
