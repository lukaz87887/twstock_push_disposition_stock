# -*- coding: utf-8 -*-
"""
notify_telegram.py — Telegram 推播 (文字訊息 + K 線 PNG)

不依賴 python-telegram-bot 的 Application (那是給互動 bot 的),
排程推播只要單純打 Telegram Bot HTTP API 即可, 更輕量。

需要環境變數:
  TELEGRAM_BOT_TOKEN — 找 @BotFather /newbot 拿到
  TELEGRAM_CHAT_ID   — 你的 chat id (找 @userinfobot 拿, 或群組 id)
"""
import os
import io
import time
import requests
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf

# 讓 matplotlib 重建字型快取, 才能認得部署時新裝的 fonts-noto-cjk
try:
    from matplotlib import font_manager
    font_manager._load_fontmanager(try_read_cache=False)
except Exception:
    pass

from core_stock import StockDataFetcher


def _cfg():
    return (os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""))


_CJK_FONT = None  # 存偵測到的中文字型名稱, 供 mplfinance style 使用


def _setup_font():
    """
    設定中文字型 (雲端 Linux 靠 nixpacks.toml 裝的 fonts-noto-cjk)。
    偵測順序:
      1. repo 內打包字型 fonts/tw_font.otf (若存在且可讀)
      2. 系統已裝的具名 CJK 字型 (Noto/微軟正黑/蘋方等)
      3. 自動掃描 matplotlib 字型清單裡任何含 CJK/Noto 關鍵字的字型
    """
    from matplotlib import font_manager

    # 1. repo 打包字型 (可選, 沒有也沒關係)
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(here, "fonts", "tw_font.otf")
    if os.path.exists(bundled):
        try:
            font_manager.fontManager.addfont(bundled)
            name = font_manager.FontProperties(fname=bundled).get_name()
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            globals()["_CJK_FONT"] = name
            print(f"[notify] 使用打包字型: {name}")
            return
        except Exception as e:
            print(f"[notify] 打包字型無法使用 ({e}), 改用系統字型")

    # 2. 系統具名 CJK 字型
    available = {f.name for f in font_manager.fontManager.ttflist}
    for c in ["Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK SC",
              "Noto Serif CJK TC", "Microsoft JhengHei", "PingFang TC",
              "WenQuanYi Micro Hei", "Noto Sans TC", "SimHei", "Droid Sans Fallback"]:
        if c in available:
            plt.rcParams["font.family"] = c
            plt.rcParams["axes.unicode_minus"] = False
            globals()["_CJK_FONT"] = c
            print(f"[notify] 使用系統字型: {c}")
            return

    # 3. 自動掃描: 任何名字含 CJK/Noto/Han 的字型
    for f in font_manager.fontManager.ttflist:
        nm = f.name.lower()
        if any(k in nm for k in ["cjk", "noto", "han", "hei", "ming", "song"]):
            plt.rcParams["font.family"] = f.name
            plt.rcParams["axes.unicode_minus"] = False
            globals()["_CJK_FONT"] = f.name
            print(f"[notify] 自動偵測到字型: {f.name}")
            return

    print("[notify] ⚠️ 找不到中文字型, 中文可能顯示為方框")
    print(f"[notify]    (系統字型清單: {sorted(available)[:15]}...)")
    plt.rcParams["axes.unicode_minus"] = False


_setup_font()


def send_message(text: str, parse_mode: str = None) -> bool:
    token, chat_id = _cfg()
    if not token or not chat_id:
        print("[notify] 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text,
               "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=20)
        return r.status_code == 200
    except Exception as e:
        print(f"[notify] send_message 失敗: {e}")
        return False


def send_photo(png_bytes: bytes, caption: str = "") -> bool:
    token, chat_id = _cfg()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        files = {"photo": ("chart.png", png_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": caption[:1000]}
        r = requests.post(url, data=data, files=files, timeout=30)
        return r.status_code == 200
    except Exception as e:
        print(f"[notify] send_photo 失敗: {e}")
        return False


def make_kline_png(ticker: str, name: str, note: str = "",
                   ma20_only: bool = False) -> bytes | None:
    """產生 K 線 PNG (台股紅漲綠跌 + 均線 + 量能)。
    上市股 (.TW) 優先用證交所 OHLCV (繞開 Yahoo SSL), 失敗再退 yfinance。"""
    df = None
    code = ticker.rsplit(".", 1)[0]
    is_twse = ticker.endswith(".TW")

    if is_twse:
        try:
            from core_stock import _fetch_twse_stock_day
            twse_df = _fetch_twse_stock_day(code, months_back=6)
            if not twse_df.empty and len(twse_df) >= 20:
                df = twse_df
            else:
                print(f"[notify] 證交所 {code} 資料不足 "
                      f"({len(twse_df) if twse_df is not None else 0} 筆), 改用 yfinance")
        except Exception as e:
            print(f"[notify] 證交所 K 線資料抓取失敗 {code}: {e}")

    if df is None:
        df = StockDataFetcher.fetch_history(ticker, period="6mo")
    if df is None or df.empty:
        return None

    # ---- 強力清理: 確保 OHLC 四欄都有值 (缺任一就整列刪), 避免 mplfinance 報錯 ----
    need_cols = ["Open", "High", "Low", "Close"]
    for c in need_cols:
        if c not in df.columns:
            print(f"[notify] {ticker} 缺 {c} 欄, 跳過繪圖")
            return None
    df = df.copy()
    cols_to_num = need_cols + (["Volume"] if "Volume" in df.columns else [])
    for c in cols_to_num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=need_cols)          # OHLC 任一缺 → 整列刪
    if "Volume" in df.columns:
        df["Volume"] = df["Volume"].fillna(0)
    if len(df) < 20:
        print(f"[notify] {ticker} 有效資料不足 20 筆 ({len(df)}), 跳過繪圖")
        return None

    plot_df = df.tail(120).copy()
    mc = mpf.make_marketcolors(up="#C62828", down="#2E7D32",
                               edge="inherit", wick="inherit",
                               volume="inherit")
    _rc = {"font.size": 9}
    if _CJK_FONT:
        _rc["font.family"] = _CJK_FONT      # ★ 綁中文字型進 mplfinance, 修圖內豆腐
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":",
                               y_on_right=True, rc=_rc)
    mav = (20,) if ma20_only else (5, 20, 60)
    buf = io.BytesIO()
    try:
        # returnfig=True 才能拿到 axes 畫處置期間網底
        fig, axes = mpf.plot(plot_df, type="candle", volume=True, mav=mav,
                             style=style, title=f"\n{code} {name}  {note}",
                             figsize=(10, 7), tight_layout=True,
                             returnfig=True)

        # ---- 畫處置期間網底 (K 線 + 量能兩個副圖都畫) ----
        _draw_disposal_spans(axes, plot_df, code)

        fig.savefig(buf, dpi=110, format="png")
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"[notify] 繪圖失敗 {ticker}: {e}")
        return None
    finally:
        plt.close("all")


def _draw_disposal_spans(axes, plot_df, code: str):
    """
    在 K 線圖與量能圖上, 對「落在圖表時間範圍內」的每一段處置期間畫紅色網底。
    只畫圖上看得到的期間 (K 線顯示到哪, 就只標到哪)。
    """
    try:
        from core_stock import get_disposal_periods
        periods = get_disposal_periods(code, days_back=200)
        if not periods:
            return

        # 圖表的時間範圍 (mplfinance 用 bar index 當 x 軸)
        idx = plot_df.index
        chart_start = idx[0]
        chart_end = idx[-1]

        # 把日期對應到 bar index
        def _to_bar_x(ts):
            ts = pd.Timestamp(ts).normalize()
            # 找最接近的 bar 位置
            pos = idx.searchsorted(ts)
            return max(0, min(pos, len(idx) - 1))

        drawn = 0
        for p in periods:
            try:
                ds = pd.Timestamp(p["start"])
                de = pd.Timestamp(p["end"])
            except Exception:
                continue
            # ★ 只畫「與圖表時間範圍有重疊」的處置期間
            if de < chart_start or ds > chart_end:
                continue
            # 裁切到圖表範圍內
            x0 = _to_bar_x(max(ds, chart_start))
            x1 = _to_bar_x(min(de, chart_end))
            if x1 <= x0:
                x1 = x0 + 0.8   # 至少畫一根寬度

            # 在所有子圖 (K線 + 量能) 都畫網底
            for ax in axes:
                if ax is None:
                    continue
                try:
                    ax.axvspan(x0 - 0.4, x1 + 0.4,
                               color="#FF6B6B", alpha=0.15,
                               zorder=0, linewidth=0)
                except Exception:
                    pass
            drawn += 1

        # 在主圖標註 (只標一次, 避免重疊)
        if drawn and axes:
            try:
                ax_main = axes[0]
                ax_main.text(
                    0.99, 0.97, "🚨 處置期間" if False else "處置期間",
                    transform=ax_main.transAxes,
                    ha="right", va="top", fontsize=9, color="#C62828",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                              edgecolor="#C62828", alpha=0.9))
            except Exception:
                pass
    except Exception as e:
        print(f"[notify] 處置網底繪製失敗 {code}: {e}")


# ==================================================================
#   組裝推播: 飆股 / 處置股
# ==================================================================
def push_momentum(result: dict, top_n: int = 10, with_charts: bool = True):
    """晚上盤後推飆股"""
    items = result.get("items", [])
    level = result.get("level", "")
    lv_name = {"basic": "Basic", "standard": "Standard", "strict": "Strict",
               "channel": "Channel", "rs_strong": "RS抗跌",
               "all": "All(綜合)"}.get(level, level)

    if result.get("error"):
        send_message(f"⚠️ 飆股掃描發生問題: {result['error']}")
    if not items:
        send_message(f"🌙 盤後飆股 [{lv_name}]\n"
                     f"今日無符合條件個股 (掃 {result.get('scanned',0)} 檔)")
        return

    header = (f"🌙 盤後飆股結算 [{lv_name}]\n"
             f"資料日 {result.get('data_date','')}  "
             f"掃描 {result.get('scanned',0)} 檔\n"
             f"符合 {len(items)} 檔, 前 {min(top_n,len(items))} 名:\n"
             f"{'━'*20}")
    lines = [header]
    for i, h in enumerate(items[:top_n], 1):
        code = h["ticker"].rsplit(".", 1)[0]
        mk = "櫃" if h["ticker"].endswith(".TWO") else "市"
        lines.append(
            f"{i}. {code}({mk}) {h['name']}  "
            f"收{h['close']} 量比{h['vol_ratio']}x\n"
            f"    {h.get('matched','')}")
    send_message("\n".join(lines))

    if with_charts:
        for h in items[:top_n]:
            code = h["ticker"].rsplit(".", 1)[0]
            png = make_kline_png(h["ticker"], h["name"],
                                 note=f"量比{h['vol_ratio']}x {h.get('matched','')}")
            if png:
                send_photo(png, caption=f"📈 {code} {h['name']}")
                time.sleep(0.5)  # 避免觸發 Telegram 限流


def push_disposal(result: dict, top_n: int = 30, with_charts: bool = True):
    """處置股推播: 統整成「一則訊息 + 一張合併大圖」"""
    items = result.get("items", [])
    added = result.get("added_today", [])
    removed = result.get("removed_today", [])
    data_date = result.get("data_date", "")

    if result.get("error"):
        send_message(f"⚠️ 處置股掃描提醒: {result['error']}")
    if not items:
        send_message(f"🚨 處置股提醒 ({data_date})\n目前無處置生效中的普通股")
        return

    # ---- 統整成單一則訊息 ----
    msg = [f"🚨 處置股提醒 ({data_date})",
           "=" * 22,
           f"📊 處置生效中共 {len(items)} 檔"]

    if added:
        msg.append(f"\n🆕 本日新增 {len(added)} 檔:")
        for it in added:
            mk = "櫃" if it.get("market") == "TWO" else "市"
            msg.append(f"  ➕ {it['code']}({mk}) {it['name']}  "
                       f"處置至 {it['disposal_end']}")
    else:
        msg.append("\n🆕 本日新增: 無")

    if removed:
        msg.append(f"\n✅ 本日出關 {len(removed)} 檔:")
        # 相容: removed 可能是 [{code,name}] 或舊的 [code字串]
        _rm = []
        for r in removed:
            if isinstance(r, dict):
                nm = r.get("name", "")
                _rm.append(f"{r['code']}{nm}" if nm else r["code"])
            else:
                _rm.append(str(r))
        msg.append("  " + "、".join(_rm))
    else:
        msg.append("\n✅ 本日出關: 無")

    # 詳細清單 (依距月線排序) — 併進同一則
    msg.append(f"\n📈 距月線排序 (🔴≤2% 🟡≤5% ⚪>5%)")
    msg.append("━" * 22)
    for r in items[:top_n]:
        mk = "櫃" if r.get("market") == "TWO" else "市"
        meas = r.get("measure", "")
        meas_s = f"  ({meas})" if meas else ""
        msg.append(
            f"{r['color']} {r['code']}({mk}) {r['name']}  "
            f"距月線{r['diff_pct']:+.1f}%\n"
            f"    處置 {r['disposal_start']}~{r['disposal_end']}{meas_s}")

    send_message("\n".join(msg))

    # ---- 一張合併大圖 (每檔一個 subplot) ----
    if with_charts:
        near = [r for r in items if r["abs_diff_pct"] <= 5][:top_n]
        if not near:
            return
        png = make_combined_kline_png(near, data_date=data_date)
        if png:
            codes = "、".join([f"{r['code']}{r['name']}" for r in near])
            send_photo(png, caption=f"📈 接近月線個股 K 線 ({len(near)} 檔)\n"
                                    f"{codes}")


# ==================================================================
#   多檔股票合併成一張大圖 (每檔一個 subplot, 越多檔圖越大)
# ==================================================================
def make_combined_kline_png(items: list, data_date: str = "") -> bytes | None:
    """
    把多檔股票的 K 線畫在同一張圖 (每檔一個 subplot)。

    items: [{code, name, market, diff_pct, disposal_start, disposal_end}, ...]
    data_date: 資料日期, 標在左上角 (避免左滑右滑看圖時錯亂)

    尺寸策略: 每個 subplot 固定 7x4.2 吋, 網格自動排列
              → 股票越多圖越大, 每張子圖都保持清晰不壓縮
    """
    if not items:
        return None

    import math
    from matplotlib import font_manager as _fm
    from matplotlib.gridspec import GridSpec
    import matplotlib.dates as mdates

    n = len(items)
    # 網格: 每列最多 2 檔 (太多列會過寬), 少於等於 2 檔就單列
    ncols = 1 if n == 1 else 2
    nrows = math.ceil(n / ncols)

    # 每個 subplot 的實體尺寸 (吋) — 固定, 所以總圖隨檔數變大
    # 加高一點容納量能副圖
    SUB_W, SUB_H = 7.5, 5.4
    # ★ 頂部空白: 要同時容納「資料日期標籤」+「第一列的子圖標題」
    #   (matplotlib 的 set_title 畫在軸框「外側上方」, 會往上侵入)
    HEADER_H = 1.4
    fig_w = SUB_W * ncols
    fig_h = SUB_H * nrows + HEADER_H

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=100, facecolor="white")
    # 外層網格: top 壓到 HEADER 下方, 讓子圖標題有空間往上長而不撞到日期標籤
    gs_outer = GridSpec(nrows, ncols, figure=fig,
                        hspace=0.45, wspace=0.20,
                        top=1 - HEADER_H / fig_h,
                        bottom=0.35 / fig_h,
                        left=0.06, right=0.97)

    # 中文字型 (各用途分別指定大小, 避免 FontProperties 覆蓋 fontsize)
    fp = None            # 一般用 (軸標籤)
    fp_title = None      # 子圖標題 (大)
    fp_date = None       # 左上角日期
    if _CJK_FONT:
        try:
            fp = _fm.FontProperties(family=_CJK_FONT, size=11)
            fp_title = _fm.FontProperties(family=_CJK_FONT, size=18,
                                          weight="bold")
            fp_date = _fm.FontProperties(family=_CJK_FONT, size=15,
                                         weight="bold")
        except Exception:
            fp = fp_title = fp_date = None

    # ---- 資料日期標示 (貼在圖片最頂端, 遠離子圖標題) ----
    _date_kw = {"fontproperties": fp_date} if fp_date else {
        "fontsize": 15, "fontweight": "bold"}
    if data_date:
        # 貼近頂邊 (留 0.35 吋), 子圖標題在 HEADER 下半部, 兩者不會撞
        y_top = 1 - (0.35 / fig_h)
        fig.text(0.015, y_top,
                 f"📅 資料日期: {data_date}",
                 ha="left", va="center", color="#1a3a5c",
                 bbox=dict(boxstyle="round,pad=0.45", facecolor="#E3F2FD",
                           edgecolor="#1565C0", alpha=0.95, linewidth=1.5),
                 **_date_kw)

    drawn = 0
    for i, it in enumerate(items):
        code = str(it["code"])
        name = it.get("name", "")
        market = it.get("market", "TW")
        ticker = f"{code}.{market}"

        df = _load_ohlcv(ticker)
        if df is None or df.empty or len(df) < 20:
            continue

        plot_df = df.tail(120).copy()
        r, c = divmod(drawn, ncols)

        # ★ 每格內再切成上下兩層: K線(3) + 量能(1)
        gs_in = gs_outer[r, c].subgridspec(4, 1, hspace=0.08)
        ax = fig.add_subplot(gs_in[0:3, 0])          # K 線主圖
        ax_vol = fig.add_subplot(gs_in[3, 0], sharex=ax)  # 量能副圖

        # --- 畫 K 線 (手繪, 才能完全控制) ---
        _draw_candles(ax, plot_df)

        # --- 20MA 月線 ---
        ma20 = plot_df["Close"].rolling(20).mean()
        ax.plot(range(len(plot_df)), ma20.values,
                color="#1E90FF", linewidth=1.8, label="20MA", zorder=3)

        # --- ★ 成交量 (紅漲綠跌) ---
        if "Volume" in plot_df.columns:
            vols = plot_df["Volume"].fillna(0).values / 1000   # 股 → 張
            vcolors = ["#C62828" if cl >= op else "#2E7D32"
                       for cl, op in zip(plot_df["Close"], plot_df["Open"])]
            ax_vol.bar(range(len(plot_df)), vols, color=vcolors,
                       width=0.7, zorder=2)
            vmax = float(vols.max()) if len(vols) else 0
            if vmax > 0:
                ax_vol.set_ylim(0, vmax * 1.15)

        # --- 處置期間網底 (K線 + 量能兩層都畫) ---
        _draw_spans_on_ax(ax, plot_df, code)
        _draw_spans_on_ax(ax_vol, plot_df, code)

        # --- 標題 ---
        mk = "櫃" if market == "TWO" else "市"
        diff = it.get("diff_pct")
        diff_s = f"  距月線{diff:+.1f}%" if diff is not None else ""
        _title_kw = {"fontproperties": fp_title} if fp_title else {
            "fontsize": 18, "fontweight": "bold"}
        ax.set_title(f"{code}({mk}) {name}{diff_s}",
                     color="#1a3a5c", pad=12, **_title_kw)

        # --- K 線主圖的軸 ---
        ax.grid(True, linestyle=":", alpha=0.35)
        ax.tick_params(labelsize=11, labelbottom=False)  # x 標籤交給量能圖
        idx = plot_df.index
        ax.set_xlim(-1, len(plot_df))
        lo, hi = plot_df["Low"].min(), plot_df["High"].max()
        pad = (hi - lo) * 0.08
        ax.set_ylim(lo - pad, hi + pad)
        _ylab_kw = {"fontproperties": fp} if fp else {"fontsize": 11}
        ax.set_ylabel("價格", **_ylab_kw)

        # --- 量能副圖的軸 ---
        ax_vol.grid(True, linestyle=":", alpha=0.3)
        ax_vol.tick_params(labelsize=10)
        step = max(len(idx) // 5, 1)
        ticks = list(range(0, len(idx), step))
        ax_vol.set_xticks(ticks)
        ax_vol.set_xticklabels([idx[t].strftime("%m/%d") for t in ticks],
                               fontsize=11, rotation=0)
        ax_vol.set_xlim(-1, len(plot_df))
        _vlab_kw = {"fontproperties": fp} if fp else {"fontsize": 10}
        ax_vol.set_ylabel("量(張)", **_vlab_kw)
        # 量能刻度用千分位
        from matplotlib.ticker import FuncFormatter
        ax_vol.yaxis.set_major_formatter(
            FuncFormatter(lambda x, _: f"{int(x):,}" if x >= 1 else ""))

        drawn += 1

    if drawn == 0:
        plt.close(fig)
        return None

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=100, facecolor="white")
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"[notify] 合併圖繪製失敗: {e}")
        return None
    finally:
        plt.close("all")


def _load_ohlcv(ticker: str):
    """抓 OHLCV (上市優先證交所, 其他退 yfinance) + 清理"""
    code = ticker.rsplit(".", 1)[0]
    df = None
    if ticker.endswith(".TW"):
        try:
            from core_stock import _fetch_twse_stock_day
            t = _fetch_twse_stock_day(code, months_back=6)
            if not t.empty and len(t) >= 20:
                df = t
        except Exception:
            pass
    if df is None:
        df = StockDataFetcher.fetch_history(ticker, period="6mo")
    if df is None or df.empty:
        return None
    need = ["Open", "High", "Low", "Close"]
    if not all(c in df.columns for c in need):
        return None
    df = df.copy()
    for c in need + (["Volume"] if "Volume" in df.columns else []):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=need)
    return df if len(df) >= 20 else None


def _draw_candles(ax, plot_df):
    """手繪 K 棒 (台股紅漲綠跌)"""
    for i, (_, row) in enumerate(plot_df.iterrows()):
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        color = "#C62828" if c >= o else "#2E7D32"
        # 影線
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        # 實體
        bottom = min(o, c)
        height = abs(c - o) or (h - l) * 0.005 or 0.01
        ax.add_patch(plt.Rectangle((i - 0.3, bottom), 0.6, height,
                                   facecolor=color, edgecolor=color,
                                   linewidth=0.5, zorder=2))


def _draw_spans_on_ax(ax, plot_df, code: str):
    """在單一 ax 上畫處置期間網底 (只畫落在圖表範圍內的)"""
    try:
        from core_stock import get_disposal_periods
        periods = get_disposal_periods(code, days_back=200)
        if not periods:
            return
        idx = plot_df.index
        c_start, c_end = idx[0], idx[-1]
        for p in periods:
            try:
                ds, de = pd.Timestamp(p["start"]), pd.Timestamp(p["end"])
            except Exception:
                continue
            if de < c_start or ds > c_end:
                continue     # 不在圖表範圍內 → 不畫
            x0 = max(0, idx.searchsorted(max(ds, c_start)))
            x1 = min(len(idx) - 1, idx.searchsorted(min(de, c_end)))
            if x1 <= x0:
                x1 = x0 + 0.8
            ax.axvspan(x0 - 0.4, x1 + 0.4, color="#FF6B6B",
                       alpha=0.16, zorder=0, linewidth=0)
    except Exception as e:
        print(f"[notify] 網底失敗 {code}: {e}")
