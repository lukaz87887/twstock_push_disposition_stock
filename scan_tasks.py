# -*- coding: utf-8 -*-
"""
scan_tasks.py — 共用掃描任務 + 結果 JSON 序列化

scheduler.py (Railway 排程) 與 streamlit_app.py (手動重掃) 都呼叫這裡,
確保「排程算的」與「手動算的」邏輯完全一致。

結果檔格式 (存到 GitHub repo 的 results/ 底下):
  results/momentum_<level>.json  ← 飆股 (各策略分開存)
  results/disposal.json          ← 處置股月線

每個 JSON:
{
  "generated_at": "2026-07-08 21:00:05",
  "data_date": "2026-07-08",
  "scanned": 1823,            # 掃了幾檔
  "level": "all",             # 飆股才有
  "items": [ {...}, {...} ]
}
"""
import os
import json
from datetime import datetime

from core_stock import (
    StrategyParams, MomentumScreener, FullMarketFetcher,
    scan_disposal_ma20,
)

RESULTS_DIR = os.environ.get("RESULTS_DIR", "results")


# ==================================================================
#   飆股掃描 (全市場上市+上櫃)
# ==================================================================
def run_momentum_scan(level: str = "all", preset: str = "standard",
                      progress_cb=None) -> dict:
    """
    完整全市場飆股掃描, 回傳結果 dict (可直接存 JSON)。
    progress_cb(stage_ratio: float, text: str) 用於顯示進度。
    """
    StrategyParams.apply_preset(preset)
    period = "2y" if level == "strict" else "6mo"

    if progress_cb:
        progress_cb(0.02, "Stage 1/3: 抓上市+上櫃全部股票清單...")
    stocks, data_date = FullMarketFetcher.fetch_stock_list(min_today_lots=0)
    if not stocks:
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_date": None, "scanned": 0, "level": level,
            "preset": preset, "items": [],
            "error": FullMarketFetcher.get_last_error() or "清單抓取失敗",
        }

    names = {f"{s['code']}.{s.get('market', 'TW')}": s["name"] for s in stocks}

    def _dl_cb(i, total, label):
        if progress_cb:
            progress_cb(0.05 + 0.55 * i / max(total, 1), f"Stage 2/3: {label}")
    frames = FullMarketFetcher.batch_download(stocks, period=period,
                                              progress_cb=_dl_cb)

    def _scan_cb(i, total, label):
        if progress_cb:
            progress_cb(0.60 + 0.40 * i / max(total, 1),
                        f"Stage 3/3: 篩選中 ({i}/{total}) {label}")
    hits = MomentumScreener.scan_frames(frames, names, level=level,
                                        progress_cb=_scan_cb)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": data_date,
        "scanned": len(frames),
        "level": level,
        "preset": preset,
        "items": hits,
        "error": None,
    }


# ==================================================================
#   處置股月線掃描 (上市+上櫃)
# ==================================================================
def run_disposal_scan(days_back: int = 30, only_active: bool = True,
                      progress_cb=None, prev_codes=None) -> dict:
    """prev_codes: {代碼: 名稱} 字典 (或舊格式的 set), 用於算本日新增/出關"""
    def _cb(i, total, label):
        if progress_cb:
            progress_cb(i / max(total, 1), f"計算月線 ({i}/{total}) {label}")
    results = scan_disposal_ma20(days_back=days_back, only_active=only_active,
                                 progress_cb=_cb)

    # disposal 內含巢狀 dict, 攤平方便 JSON 存取與顯示
    items = []
    for r in results:
        d = r["disposal"]
        items.append({
            "code": r["code"], "name": r["name"],
            "market": d.get("market", "TW"),
            "close": r["close"], "ma20": r["ma20"],
            "diff_pct": r["diff_pct"], "abs_diff_pct": r["abs_diff_pct"],
            "color": r["color"], "change_5d_pct": r["change_5d_pct"],
            "disposal_start": d["disposal_start"],
            "disposal_end": d["disposal_end"],
            "measure": d.get("measure", ""),
            "is_active": d.get("is_active", False),
        })

    # ---- 本日新增 / 本日出關 比對 ----
    today_codes = {it["code"] for it in items}
    added, removed = [], []
    if prev_codes is not None:
        # 相容: prev_codes 可能是 dict {code: name} 或舊的 set
        if isinstance(prev_codes, dict):
            prev_map = prev_codes
        else:
            prev_map = {c: "" for c in prev_codes}

        added = [it for it in items if it["code"] not in prev_map]
        # ★ 出關名單帶上中文名稱 (從昨天的資料撈)
        removed = [{"code": c, "name": prev_map.get(c, "")}
                   for c in sorted(set(prev_map) - today_codes)]

    from core_stock import DisposalStockFetcher
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": datetime.now().strftime("%Y-%m-%d"),
        "scanned": len(items),
        "items": items,
        "added_today": added,          # 本日新增 (完整 item)
        "removed_today": removed,      # 本日出關 [{code, name}, ...]
        "all_codes": sorted(today_codes),
        "error": DisposalStockFetcher.get_last_error() or None,
    }


# ==================================================================
#   JSON 存 / 讀
# ==================================================================
def save_result(kind: str, data: dict) -> str:
    """kind: 'momentum_all' / 'disposal' 等。回傳存檔路徑。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{kind}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_result(kind: str) -> dict | None:
    path = os.path.join(RESULTS_DIR, f"{kind}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
