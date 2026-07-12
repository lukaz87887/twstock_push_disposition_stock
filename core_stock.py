# -*- coding: utf-8 -*-
"""
core_stock.py v2 — 台股篩選核心邏輯 (完整移植自桌面版 v9.4.2)

v2 更新:
  • StrategyParams: 三種預設組 (保守/標準/寬鬆), 與桌面版參數完全一致
  • MomentumScreener: 完整六策略
      basic / standard / strict / channel / rs_strong / all
  • DisposalStockFetcher + 月線距離 (不變)
零 UI 依賴, Streamlit 與 Telegram Bot 共用。
"""
import os
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from scipy.stats import linregress

# 六種策略定義 (給 UI 用)
STRATEGY_LEVELS = [
    ("basic",     "🟢 Basic — 多頭排列 + 帶量突破",
     "原始 3 條件: 多頭排列 + 突破 20 日高 + 流動性"),
    ("standard",  "🟡 Standard — 含動能濾鏡",
     "Basic + RSI 區間 + MACD 動能 + 不可追高 (離 50MA)"),
    ("strict",    "🔴 Strict — Minervini 飆股原型",
     "Standard + 150/200MA + 52週高低 + RS Rating (較慢, 抓 2 年資料)"),
    ("channel",   "📐 Channel — 下降通道突破 (旗桿型態)",
     "120 天找高點, 擬合下降壓力線, 帶量突破"),
    ("rs_strong", "🛡️ RS Strong — 大盤大跌抗跌",
     "過去 90 天大盤跌 >2% 時個股逆勢收紅 ≥3 次 (含最新一次)"),
    ("all",       "🔥 All — 任一策略成立 (訊號最多)",
     "Standard ∪ Channel ∪ RS Strong, 廣撒網"),
]


# ==================================================================
#   StrategyParams — 三種預設組 (與桌面版一致)
# ==================================================================
class StrategyParams:
    _defaults = {
        "vol_multiplier":         2.0,
        "min_vol_lots":           1000,
        "channel_vol_multiplier": 1.5,
        "breakout_window":        20,
        "rsi_min":                50,
        "rsi_max":                75,
        "require_macd_positive":  True,
        "max_extension_pct":      25,
        "channel_lookback":       120,
        "channel_min_len":        30,
        "channel_min_r2":         0.3,
        "channel_min_slope":      -0.02,
        "rs_lookback":            90,
        "market_drop_threshold":  -0.02,
        "rs_min_count":           3,
        "rs_max_count":           15,
        "high_52w_min_pct":       0.75,
        "low_52w_min_mult":       1.30,
        "rs_rating_min":          70,
    }
    _presets = {
        "conservative": {   # 🛡️ 保守
            "vol_multiplier": 2.5, "min_vol_lots": 2000,
            "channel_vol_multiplier": 2.0, "breakout_window": 30,
            "rsi_min": 55, "rsi_max": 70, "require_macd_positive": True,
            "max_extension_pct": 15, "channel_min_len": 40,
            "channel_min_r2": 0.4, "channel_min_slope": -0.03,
            "market_drop_threshold": -0.025, "rs_min_count": 5,
            "high_52w_min_pct": 0.85, "low_52w_min_mult": 1.40,
            "rs_rating_min": 80,
        },
        "standard": {},     # ⚖️ 標準 = 預設值
        "aggressive": {     # 🔥 寬鬆
            "vol_multiplier": 1.5, "min_vol_lots": 500,
            "channel_vol_multiplier": 1.2, "breakout_window": 15,
            "rsi_min": 45, "rsi_max": 85, "require_macd_positive": False,
            "max_extension_pct": 35, "channel_min_len": 20,
            "channel_min_r2": 0.2, "channel_min_slope": -0.01,
            "market_drop_threshold": -0.015, "rs_min_count": 2,
            "high_52w_min_pct": 0.65, "low_52w_min_mult": 1.20,
            "rs_rating_min": 60,
        },
    }
    PRESET_LABELS = {
        "conservative": "🛡️ 保守 (訊號少, 品質高)",
        "standard":     "⚖️ 標準 (平衡預設)",
        "aggressive":   "🔥 寬鬆 (訊號多, 廣撒網)",
    }
    _current: dict = None

    @classmethod
    def get(cls) -> dict:
        if cls._current is None:
            cls._current = cls._defaults.copy()
        return cls._current.copy()

    @classmethod
    def set_batch(cls, updates: dict):
        if cls._current is None:
            cls._current = cls._defaults.copy()
        cls._current.update(updates)

    @classmethod
    def apply_preset(cls, name: str) -> dict:
        cls._current = cls._defaults.copy()
        if name in cls._presets:
            cls._current.update(cls._presets[name])
        return cls._current.copy()

    @classmethod
    def preset_values(cls, name: str) -> dict:
        base = cls._defaults.copy()
        base.update(cls._presets.get(name, {}))
        return base


# ==================================================================
#   股價抓取 (yfinance + 記憶體快取)
# ==================================================================
class StockDataFetcher:
    _cache: dict = {}
    _session = None

    @classmethod
    def _get_session(cls):
        """建立一個關閉 SSL 驗證問題的 requests session (解 Railway 上 fc.yahoo.com SSL 錯誤)"""
        if cls._session is None:
            try:
                import requests as _rq
                s = _rq.Session()
                s.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36"
                })
                cls._session = s
            except Exception:
                cls._session = False
        return cls._session or None

    @classmethod
    def fetch_history(cls, ticker: str, period: str = "6mo",
                      force_refresh: bool = False) -> pd.DataFrame:
        today_key = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"{ticker}_{period}_{today_key}"
        if not force_refresh and cache_key in cls._cache:
            return cls._cache[cache_key].copy()

        # 嘗試多種方式抓取 (解決雲端 SSL / curl_cffi 問題)
        df = cls._try_fetch(ticker, period)
        if df is None or df.empty:
            return pd.DataFrame()
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.index = df.index.normalize()
        except Exception:
            pass
        cls._cache[cache_key] = df.copy()
        return df

    @classmethod
    def _try_fetch(cls, ticker: str, period: str):
        # 方式 1: 標準 yf.Ticker
        try:
            df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
            if not df.empty:
                return df
        except Exception:
            pass
        # 方式 2: yf.download (有時較穩)
        try:
            df = yf.download(ticker, period=period, auto_adjust=False,
                             progress=False, threads=False)
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
        except Exception:
            pass
        return None

    @classmethod
    def clear_cache(cls):
        cls._cache.clear()


# ==================================================================
#   六策略篩選器 (完整移植)
# ==================================================================
class MomentumScreener:
    LONG_MA = 60

    # ---------- 指標 ----------
    @classmethod
    def compute_indicators(cls, df: pd.DataFrame) -> pd.DataFrame:
        p = StrategyParams.get()
        out = df.copy()
        out["MA5"]  = out["Close"].rolling(5).mean()
        out["MA20"] = out["Close"].rolling(20).mean()
        out["MA60"] = out["Close"].rolling(60).mean()
        out["MA50"] = out["Close"].rolling(50).mean()
        out["VolMA20"] = out["Volume"].rolling(20).mean()
        out["High20Prev"] = out["High"].rolling(
            int(p["breakout_window"])).max().shift(1)
        out["RSI14"] = cls._calc_rsi(out["Close"], 14)
        macd, sig, hist = cls._calc_macd(out["Close"])
        out["MACD"], out["MACDsig"], out["MACDhist"] = macd, sig, hist
        # Strict 用
        out["MA150"] = out["Close"].rolling(150).mean()
        out["MA200"] = out["Close"].rolling(200).mean()
        out["MA200_30dAgo"] = out["MA200"].shift(30)
        out["High52w"] = out["High"].rolling(252, min_periods=60).max()
        out["Low52w"]  = out["Low"].rolling(252, min_periods=60).min()
        return out

    @staticmethod
    def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1.0/period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1.0/period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _calc_macd(close, fast=12, slow=26, signal=9):
        ema_f = close.ewm(span=fast, adjust=False).mean()
        ema_s = close.ewm(span=slow, adjust=False).mean()
        macd = ema_f - ema_s
        sig = macd.ewm(span=signal, adjust=False).mean()
        return macd, sig, macd - sig

    # ---------- 三等級條件 ----------
    @classmethod
    def _check_basic(cls, row) -> bool:
        p = StrategyParams.get()
        try:
            close, vol = row["Close"], row["Volume"]
            ma5, ma20, ma60 = row["MA5"], row["MA20"], row["MA60"]
            vol_ma20, high20 = row["VolMA20"], row["High20Prev"]
        except KeyError:
            return False
        if pd.isna([ma5, ma20, ma60, vol_ma20, high20]).any():
            return False
        return (close > ma5 > ma20 > ma60
                and close > high20
                and vol > vol_ma20 * p["vol_multiplier"]
                and vol_ma20 >= p["min_vol_lots"] * 1000)

    @classmethod
    def _check_standard(cls, row) -> bool:
        if not cls._check_basic(row):
            return False
        p = StrategyParams.get()
        try:
            close, ma50 = row["Close"], row["MA50"]
            rsi, hist = row["RSI14"], row["MACDhist"]
        except KeyError:
            return False
        if pd.isna([ma50, rsi, hist]).any():
            return False
        if not (p["rsi_min"] <= rsi <= p["rsi_max"]):
            return False
        if p["require_macd_positive"] and hist <= 0:
            return False
        if (close - ma50) / ma50 > p["max_extension_pct"] / 100:
            return False
        return True

    @classmethod
    def _check_strict(cls, row, rs_rating=None) -> bool:
        if not cls._check_standard(row):
            return False
        p = StrategyParams.get()
        try:
            close = row["Close"]
            ma50, ma150, ma200 = row["MA50"], row["MA150"], row["MA200"]
            ma200_30d = row["MA200_30dAgo"]
            high52, low52 = row["High52w"], row["Low52w"]
        except KeyError:
            return False
        if pd.isna([ma150, ma200, ma200_30d, high52, low52]).any():
            return False
        if not (close > ma150 and close > ma200):
            return False
        if not (ma150 > ma200):
            return False
        if not (ma200 > ma200_30d):
            return False
        if not (ma50 > ma150 > ma200):
            return False
        if not (close >= low52 * p["low_52w_min_mult"]):
            return False
        if not (close >= high52 * p["high_52w_min_pct"]):
            return False
        if rs_rating is not None and rs_rating < p["rs_rating_min"]:
            return False
        return True

    @staticmethod
    def calc_rs_rating(df, benchmark_df, lookback=126) -> float:
        if len(df) < lookback + 1 or len(benchmark_df) < lookback + 1:
            return 50.0
        try:
            s = df["Close"].iloc[-1] / df["Close"].iloc[-lookback] - 1
            b = benchmark_df["Close"].iloc[-1] / benchmark_df["Close"].iloc[-lookback] - 1
        except (IndexError, KeyError):
            return 50.0
        return float(max(0, min(100, 50 + (s - b) * 100)))

    # ---------- basic / standard / strict 即時檢查 ----------
    @classmethod
    def check_stock(cls, ticker, name, level="basic", benchmark_df=None):
        df = StockDataFetcher.fetch_history(
            ticker, period="2y" if level == "strict" else "6mo")
        min_req = 250 if level == "strict" else cls.LONG_MA + 5
        if df.empty or len(df) < min_req:
            return None
        df = cls.compute_indicators(df)
        last = df.iloc[-1]

        rs_rating = None
        if level == "strict" and benchmark_df is not None:
            rs_rating = cls.calc_rs_rating(df, benchmark_df)

        ok = (cls._check_basic(last) if level == "basic"
              else cls._check_standard(last) if level == "standard"
              else cls._check_strict(last, rs_rating))
        if not ok:
            return None
        return {
            "ticker": ticker, "name": name,
            "close": round(float(last["Close"]), 2),
            "volume": int(last["Volume"]),
            "vol_ratio": round(float(last["Volume"] / last["VolMA20"]), 2)
                         if last["VolMA20"] else 0,
            "rsi": round(float(last.get("RSI14", 0)), 1),
            "rs_rating": round(rs_rating, 1) if rs_rating else None,
            "level": level, "matched": level,
        }

    # ---------- Channel 通道突破 ----------
    @classmethod
    def _check_channel_row(cls, df, end_idx=None):
        p = StrategyParams.get()
        if end_idx is None:
            end_idx = len(df) - 1
        lookback = int(p["channel_lookback"])
        if end_idx < lookback:
            return None
        sub = df.iloc[max(0, end_idx - lookback):end_idx + 1]
        if len(sub) < max(100, int(p["channel_min_len"]) + 10):
            return None
        vol_ma20 = sub["Volume"].tail(20).mean()
        if vol_ma20 < p["min_vol_lots"] * 1000:
            return None
        ma90 = sub["Close"].rolling(90).mean()
        if pd.isna(ma90.iloc[-1]) or pd.isna(ma90.iloc[-20]):
            return None
        if ma90.iloc[-1] <= ma90.iloc[-20]:
            return None
        peak_idx = sub["High"].argmax()
        if peak_idx >= len(sub) - 5:
            return None
        corr = sub.iloc[peak_idx:-1]
        if len(corr) < p["channel_min_len"]:
            return None
        x = np.arange(len(corr))
        try:
            slope, intercept, r, _, _ = linregress(x, corr["High"].values)
        except Exception:
            return None
        if slope >= p["channel_min_slope"] or r ** 2 < p["channel_min_r2"]:
            return None
        today = sub.iloc[-1]
        resistance = slope * len(corr) + intercept
        if today["Close"] <= resistance:
            return None
        if today["Volume"] < vol_ma20 * p["channel_vol_multiplier"]:
            return None
        return {
            "today_close": round(float(today["Close"]), 2),
            "today_volume": int(today["Volume"]),
            "vol_ma20": int(vol_ma20),
            "channel_days": len(corr),
            "r_squared": round(float(r ** 2), 2),
        }

    @classmethod
    def check_channel_breakout(cls, ticker, name):
        df = StockDataFetcher.fetch_history(ticker, period="1y")
        if df.empty or len(df) < 120:
            return None
        res = cls._check_channel_row(df)
        if res is None:
            return None
        return {
            "ticker": ticker, "name": name,
            "close": res["today_close"], "volume": res["today_volume"],
            "vol_ratio": round(res["today_volume"] / res["vol_ma20"], 2)
                         if res["vol_ma20"] else 0,
            "rsi": 0, "rs_rating": None,
            "level": "channel",
            "matched": f"通道{res['channel_days']}天 R²={res['r_squared']}",
        }

    # ---------- RS Strong 抗跌 ----------
    @classmethod
    def get_market_crash_dates(cls):
        p = StrategyParams.get()
        try:
            twii = yf.Ticker("^TWII").history(period="6mo")
            if twii.empty:
                return set(), None
            if twii.index.tz is not None:
                twii.index = twii.index.tz_localize(None)
            twii["PctChg"] = twii["Close"].pct_change()
            crashes = twii[twii["PctChg"] <= p["market_drop_threshold"]] \
                .tail(int(p["rs_lookback"]))
            dates = sorted(set(crashes.index.strftime("%Y-%m-%d")))
            return set(dates), (dates[-1] if dates else None)
        except Exception:
            return set(), None

    @classmethod
    def check_rs_strong(cls, ticker, name, crash_dates=None, latest_crash=None):
        p = StrategyParams.get()
        if crash_dates is None:
            crash_dates, latest_crash = cls.get_market_crash_dates()
            if not crash_dates:
                return None
        df = StockDataFetcher.fetch_history(ticker, period="6mo")
        if df.empty or len(df) < p["rs_lookback"] + 5:
            return None
        if df["Volume"].tail(5).mean() < p["min_vol_lots"] * 1000:
            return None
        if df["Close"].tail(30).mean() <= df["Close"].tail(60).mean():
            return None
        recent = df.tail(int(p["rs_lookback"])).copy()
        recent["PrevClose"] = df["Close"].shift(1).loc[recent.index]
        rs_dates = [d.strftime("%Y-%m-%d") for d, row in recent.iterrows()
                    if d.strftime("%Y-%m-%d") in crash_dates
                    and row["Close"] > row["PrevClose"]]
        count = len(rs_dates)
        if not (p["rs_min_count"] <= count <= p["rs_max_count"]):
            return None
        if latest_crash and latest_crash not in rs_dates:
            return None
        last = df.iloc[-1]
        return {
            "ticker": ticker, "name": name,
            "close": round(float(last["Close"]), 2),
            "volume": int(last["Volume"]),
            "vol_ratio": round(float(last["Volume"] /
                               df["Volume"].tail(20).mean()), 2),
            "rsi": 0, "rs_rating": None,
            "level": "rs_strong",
            "matched": f"抗跌 {count} 次",
        }

    # =====================================================
    #   v3: 以現成 DataFrame 檢查 (全市場批次掃描用)
    # =====================================================
    @classmethod
    def _check_rs_strong_df(cls, df, ticker, name, crash_dates, latest_crash):
        p = StrategyParams.get()
        if df is None or df.empty or len(df) < p["rs_lookback"] + 5:
            return None
        if df["Volume"].tail(5).mean() < p["min_vol_lots"] * 1000:
            return None
        if df["Close"].tail(30).mean() <= df["Close"].tail(60).mean():
            return None
        recent = df.tail(int(p["rs_lookback"])).copy()
        recent["PrevClose"] = df["Close"].shift(1).loc[recent.index]
        rs_dates = [d.strftime("%Y-%m-%d") for d, row in recent.iterrows()
                    if d.strftime("%Y-%m-%d") in crash_dates
                    and row["Close"] > row["PrevClose"]]
        count = len(rs_dates)
        if not (p["rs_min_count"] <= count <= p["rs_max_count"]):
            return None
        if latest_crash and latest_crash not in rs_dates:
            return None
        last = df.iloc[-1]
        return {
            "ticker": ticker, "name": name,
            "close": round(float(last["Close"]), 2),
            "volume": int(last["Volume"]),
            "vol_ratio": round(float(last["Volume"] /
                               max(df["Volume"].tail(20).mean(), 1)), 2),
            "rsi": 0, "rs_rating": None,
            "level": "rs_strong", "matched": f"抗跌 {count} 次",
        }

    @classmethod
    def check_from_df(cls, df, ticker, name, level="basic",
                      benchmark_df=None, crash_dates=None, latest_crash=None):
        """統一入口: 給定 OHLCV DataFrame, 依 level 檢查 (支援全部六策略)"""
        if df is None or df.empty:
            return None

        if level in ("basic", "standard", "strict"):
            min_req = 250 if level == "strict" else cls.LONG_MA + 5
            if len(df) < min_req:
                return None
            ind = cls.compute_indicators(df)
            last = ind.iloc[-1]
            rs_rating = None
            if level == "strict" and benchmark_df is not None:
                rs_rating = cls.calc_rs_rating(df, benchmark_df)
            ok = (cls._check_basic(last) if level == "basic"
                  else cls._check_standard(last) if level == "standard"
                  else cls._check_strict(last, rs_rating))
            if not ok:
                return None
            return {
                "ticker": ticker, "name": name,
                "close": round(float(last["Close"]), 2),
                "volume": int(last["Volume"]),
                "vol_ratio": round(float(last["Volume"] / last["VolMA20"]), 2)
                             if last["VolMA20"] else 0,
                "rsi": round(float(last.get("RSI14", 0) or 0), 1),
                "rs_rating": round(rs_rating, 1) if rs_rating else None,
                "level": level, "matched": level,
            }

        if level == "channel":
            if len(df) < 120:
                return None
            res = cls._check_channel_row(df)
            if res is None:
                return None
            return {
                "ticker": ticker, "name": name,
                "close": res["today_close"], "volume": res["today_volume"],
                "vol_ratio": round(res["today_volume"] /
                                   max(res["vol_ma20"], 1), 2),
                "rsi": 0, "rs_rating": None, "level": "channel",
                "matched": f"通道{res['channel_days']}天 R²={res['r_squared']}",
            }

        if level == "rs_strong":
            if not crash_dates:
                return None
            return cls._check_rs_strong_df(df, ticker, name,
                                           crash_dates, latest_crash)

        if level == "all":
            matched, info = [], None
            r = cls.check_from_df(df, ticker, name, "standard")
            if r:
                matched.append("Standard"); info = r
            r2 = cls.check_from_df(df, ticker, name, "channel")
            if r2:
                matched.append("Channel"); info = info or r2
            if crash_dates:
                r3 = cls._check_rs_strong_df(df, ticker, name,
                                             crash_dates, latest_crash)
                if r3:
                    matched.append("RS強"); info = info or r3
            if info:
                info = dict(info)
                info["matched"] = " + ".join(matched)
            return info
        return None

    @classmethod
    def scan_frames(cls, frames: dict, names: dict, level="basic",
                    progress_cb=None):
        """全市場掃描: 對已下載的 {code: df} 逐一跑 check_from_df"""
        benchmark_df = None
        crash_dates, latest_crash = None, None
        if level == "strict":
            benchmark_df = StockDataFetcher.fetch_history("^TWII", period="2y")
        if level in ("rs_strong", "all"):
            crash_dates, latest_crash = cls.get_market_crash_dates()

        hits = []
        total = len(frames)
        for i, (ticker, df) in enumerate(frames.items(), 1):
            if progress_cb and (i % 25 == 0 or i == total):
                progress_cb(i, total, f"{ticker} {names.get(ticker, '')}")
            try:
                info = cls.check_from_df(
                    df, ticker, names.get(ticker, ticker), level,
                    benchmark_df, crash_dates, latest_crash)
            except Exception:
                info = None
            if info:
                hits.append(info)
        hits.sort(key=lambda x: x["vol_ratio"], reverse=True)
        return hits



# ==================================================================
#   全市場抓取器 (v3 新增) — 兩段式全上市掃描
#   Stage 1: 證交所 MI_INDEX 一個請求拿全部上市股清單+當日量 (流動性預過濾)
#   Stage 2: yfinance 批次下載 (一次 150 檔, 多執行緒)
# ==================================================================
class FullMarketFetcher:
    MI_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    _last_error = ""

    @classmethod
    def get_last_error(cls):
        return cls._last_error

    @staticmethod
    def _is_common_stock(code: str) -> bool:
        code = str(code).strip()
        return len(code) == 4 and code.isdigit() and not code.startswith("00")

    @staticmethod
    def _extract_quote_table(payload):
        """MI_INDEX 回傳格式歷經改版, 兩種都支援"""
        # 新版: payload["tables"] = [{title, fields, data}, ...]
        for t in payload.get("tables", []):
            f = t.get("fields", [])
            if "證券代號" in f and "收盤價" in f:
                return f, t.get("data", [])
        # 舊版: fields9/data9, fields8/data8 ...
        for i in range(9, 0, -1):
            f = payload.get(f"fields{i}")
            d = payload.get(f"data{i}")
            if f and d and "證券代號" in f:
                return f, d
        return None, None

    TPEX_QUOTES_URL = ("https://www.tpex.org.tw/openapi/v1/"
                       "tpex_mainboard_daily_close_quotes")

    @staticmethod
    def _resolve_key(d: dict, *cands):
        for k in d.keys():
            ks = str(k); kl = ks.lower()
            for c in cands:
                if c in ks or c.lower() in kl:
                    return k
        return None

    @classmethod
    def _fetch_tpex_list(cls, min_today_lots: int = 0):
        """上櫃全部普通股 (櫃買中心 OpenAPI, key 名稱容錯解析)"""
        r = requests.get(cls.TPEX_QUOTES_URL,
                         headers={**cls.HEADERS,
                                  'Accept': 'application/json'},
                         timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"TPEX HTTP {r.status_code}")
        arr = r.json()
        if not isinstance(arr, list) or not arr:
            raise RuntimeError("TPEX 回傳空清單")
        s = arr[0]
        k_code = cls._resolve_key(s, "SecuritiesCompanyCode", "Code", "代號")
        k_name = cls._resolve_key(s, "CompanyName", "Name", "名稱")
        k_vol = cls._resolve_key(s, "TradingShares", "TradeVolume",
                                 "成交股數", "Shares")
        if not k_code:
            raise RuntimeError(f"TPEX 欄位無法辨識: {list(s.keys())}")
        stocks = []
        for item in arr:
            code = str(item.get(k_code, "")).strip()
            if not cls._is_common_stock(code):
                continue
            try:
                vol = int(str(item.get(k_vol, "0")).replace(",", "")) // 1000
            except (ValueError, TypeError):
                vol = 0
            if vol < min_today_lots:
                continue
            stocks.append({"code": code,
                           "name": str(item.get(k_name, "")).strip(),
                           "today_lots": vol, "market": "TWO"})
        return stocks

    @classmethod
    def fetch_stock_list(cls, min_today_lots: int = 0):
        """
        抓最近交易日「上市 + 上櫃」全部普通股清單。
        v4: 預設 min_today_lots=0 = 完全不預過濾, 避免任何漏網之魚
            (流動性條件交由各策略自己判斷)
        Returns: (list[{code, name, today_lots, market}], date_str)
        """
        cls._last_error = ""
        for back in range(0, 10):
            d = datetime.now() - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            ds = d.strftime("%Y%m%d")
            try:
                r = requests.get(cls.MI_URL, params={
                    "response": "json", "date": ds, "type": "ALLBUT0999",
                }, headers=cls.HEADERS, timeout=30)
                if r.status_code != 200:
                    continue
                payload = r.json()
                if payload.get("stat") != "OK":
                    continue
                fields, data = cls._extract_quote_table(payload)
                if not fields:
                    cls._last_error = "找不到行情表 (API 格式改版?)"
                    continue
                i_code = fields.index("證券代號")
                i_name = fields.index("證券名稱")
                i_vol = fields.index("成交股數")
                stocks = []
                for row in data:
                    code = str(row[i_code]).strip()
                    if not cls._is_common_stock(code):
                        continue
                    try:
                        vol = int(str(row[i_vol]).replace(",", "")) // 1000
                    except (ValueError, TypeError):
                        vol = 0
                    if vol < min_today_lots:
                        continue
                    stocks.append({"code": code,
                                   "name": str(row[i_name]).strip(),
                                   "today_lots": vol, "market": "TW"})
                if stocks:
                    # ★ v4: 追加上櫃 (失敗不影響上市結果, 但記錄警告)
                    try:
                        stocks += cls._fetch_tpex_list(min_today_lots)
                    except Exception as e:
                        cls._last_error = f"上櫃清單抓取失敗(僅掃上市): {e}"
                    return stocks, d.strftime("%Y-%m-%d")
            except Exception as e:
                cls._last_error = str(e)
                continue
        # ---- 備援: OpenAPI STOCK_DAY_ALL (雲端/海外 IP 友善) ----
        try:
            return cls._fetch_list_via_openapi(min_today_lots)
        except Exception as e:
            cls._last_error = (f"主要API失敗: {cls._last_error}  |  "
                               f"備援OpenAPI: {e}")
        return [], None

    @classmethod
    def _fetch_list_via_openapi(cls, min_today_lots: int):
        """備援: openapi.twse.com.tw STOCK_DAY_ALL — 最近交易日全部上市個股"""
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        r = requests.get(url, headers={**cls.HEADERS,
                                       'Accept': 'application/json'},
                         timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        arr = r.json()
        if not isinstance(arr, list) or not arr:
            raise RuntimeError("回傳空清單")
        sample = arr[0]
        def _k(*cands):
            for k in sample.keys():
                ks = str(k); kl = ks.lower()
                for c in cands:
                    if c in ks or c.lower() in kl:
                        return k
            return None
        k_code = _k("Code", "代號")
        k_name = _k("Name", "名稱")
        k_vol = _k("TradeVolume", "成交股數")
        if not (k_code and k_vol):
            raise RuntimeError(f"欄位無法辨識: {list(sample.keys())}")
        stocks = []
        for item in arr:
            code = str(item.get(k_code, "")).strip()
            if not cls._is_common_stock(code):
                continue
            try:
                vol = int(str(item.get(k_vol, "0")).replace(",", "")) // 1000
            except (ValueError, TypeError):
                vol = 0
            if vol < min_today_lots:
                continue
            stocks.append({"code": code,
                           "name": str(item.get(k_name, "")).strip(),
                           "today_lots": vol, "market": "TW"})
        if not stocks:
            raise RuntimeError("過濾後 0 檔")
        try:
            stocks += cls._fetch_tpex_list(min_today_lots)
        except Exception:
            pass
        return stocks, datetime.now().strftime("%Y-%m-%d")

    @classmethod
    def batch_download(cls, stock_list: list, period: str = "6mo",
                       progress_cb=None, chunk_size: int = 150) -> dict:
        """yfinance 批次下載 (上市 .TW / 上櫃 .TWO)
        stock_list: [{code, market}, ...]
        回傳 {ticker: OHLCV DataFrame} (ticker 含後綴)"""
        frames = {}
        tickers = [f"{s['code']}.{s.get('market', 'TW')}"
                   for s in stock_list]
        n_chunks = (len(tickers) + chunk_size - 1) // chunk_size
        for ci in range(n_chunks):
            chunk = tickers[ci * chunk_size:(ci + 1) * chunk_size]
            if progress_cb:
                progress_cb(ci + 1, n_chunks,
                            f"批次下載 {ci + 1}/{n_chunks} ({len(chunk)} 檔)")
            try:
                data = yf.download(chunk, period=period, auto_adjust=False,
                                   group_by="ticker", threads=True,
                                   progress=False)
            except Exception:
                continue
            if data is None or len(data) == 0:
                continue
            multi = isinstance(data.columns, pd.MultiIndex)
            for tk in chunk:
                try:
                    if multi:
                        if tk not in set(data.columns.get_level_values(0)):
                            continue
                        df = data[tk].copy()
                    else:
                        df = data.copy()
                    df = df.dropna(subset=["Close"])
                    if df.empty or len(df) < 30:
                        continue
                    df["Volume"] = df["Volume"].fillna(0)
                    if getattr(df.index, "tz", None) is not None:
                        df.index = df.index.tz_localize(None)
                    df.index = pd.to_datetime(df.index).normalize()
                    frames[tk] = df
                except Exception:
                    continue
        return frames


# ==================================================================
#   處置股 + 月線 (與 v1 相同)
# ==================================================================
class DisposalStockFetcher:
    API_URL = "https://www.twse.com.tw/announcement/punish"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    _last_error = ""

    @classmethod
    def get_last_error(cls):
        return cls._last_error

    @staticmethod
    def _is_common_stock(code: str) -> bool:
        code = str(code).strip()
        return len(code) == 4 and code.isdigit() and not code.startswith("00")

    @staticmethod
    def _roc_to_western(roc: str):
        try:
            parts = roc.strip().replace("-", "/").split("/")
            if len(parts) != 3:
                return None
            return f"{int(parts[0]) + 1911:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except (ValueError, IndexError):
            return None

    OPENAPI_URL = "https://openapi.twse.com.tw/v1/announcement/punish"

    @staticmethod
    def _roc_any_to_western(s: str):
        """支援 115/07/07 與 1150707 兩種民國格式"""
        s = str(s).strip()
        if "/" in s or "-" in s:
            return DisposalStockFetcher._roc_to_western(s)
        if s.isdigit() and len(s) == 7:
            return DisposalStockFetcher._roc_to_western(
                f"{s[:3]}/{s[3:5]}/{s[5:7]}")
        return None

    @staticmethod
    def _resolve_key(d: dict, *cands):
        """在 dict 的 key 裡找出包含任一候選字串的 key (中英通吃)"""
        for k in d.keys():
            ks = str(k); kl = ks.lower()
            for c in cands:
                if c in ks or c.lower() in kl:
                    return k
        return None

    @classmethod
    def _parse_period(cls, period: str):
        period = str(period).strip()
        for sep in ["～", "~", "—", "-"]:
            if sep in period:
                parts = period.split(sep)
                break
        else:
            parts = [period]
        ds = cls._roc_any_to_western(parts[0]) if parts else None
        de = cls._roc_any_to_western(parts[1]) if len(parts) > 1 else None
        return ds, de

    @classmethod
    def _fetch_via_website(cls, days_back: int) -> list:
        """主要來源: www.twse.com.tw (台灣 IP 最穩)"""
        end = datetime.now()
        start = end - timedelta(days=days_back)
        resp = requests.get(cls.API_URL, params={
            "response": "json",
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
        }, headers=cls.HEADERS, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        payload = resp.json()
        if payload.get("stat") != "OK":
            raise RuntimeError(f"stat={payload.get('stat')}")
        today_str = datetime.now().strftime("%Y-%m-%d")
        seen = {}
        for row in payload.get("data", []):
            if len(row) < 9:
                continue
            code = str(row[2]).strip()
            if not cls._is_common_stock(code):
                continue
            ds, de = cls._parse_period(str(row[6]))
            rec = {
                "code": code, "name": str(row[3]).strip(),
                "publish_date": cls._roc_to_western(str(row[1])) or "",
                "disposal_start": ds or "", "disposal_end": de or "",
                "condition": str(row[5]).strip(),
                "measure": str(row[7]).strip(),
                "is_active": bool(ds and de and ds <= today_str <= de),
            }
            if code not in seen or rec["disposal_start"] > seen[code]["disposal_start"]:
                seen[code] = rec
        return list(seen.values())

    @classmethod
    def _fetch_via_openapi(cls) -> list:
        """備援來源: openapi.twse.com.tw (雲端主機友善, 海外 IP 可用)
        回傳格式為 JSON array of dict, key 名稱用容錯搜尋 (中英通吃)"""
        resp = requests.get(cls.OPENAPI_URL,
                            headers={**cls.HEADERS,
                                     'Accept': 'application/json'},
                            timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"OpenAPI HTTP {resp.status_code}")
        arr = resp.json()
        if not isinstance(arr, list) or not arr:
            raise RuntimeError("OpenAPI 回傳空清單")
        today_str = datetime.now().strftime("%Y-%m-%d")
        sample = arr[0]
        k_code = cls._resolve_key(sample, "證券代號", "代號", "Code")
        k_name = cls._resolve_key(sample, "證券名稱", "名稱", "Name")
        k_period = cls._resolve_key(sample, "起訖", "期間", "Period")
        k_pub = cls._resolve_key(sample, "公布", "Date", "日期")
        k_cond = cls._resolve_key(sample, "條件", "Condition", "Criteria")
        k_meas = cls._resolve_key(sample, "措施", "Measure", "Disposition")
        if not (k_code and k_period):
            raise RuntimeError(f"OpenAPI 欄位無法辨識: {list(sample.keys())}")
        seen = {}
        for item in arr:
            code = str(item.get(k_code, "")).strip()
            if not cls._is_common_stock(code):
                continue
            ds, de = cls._parse_period(item.get(k_period, ""))
            rec = {
                "code": code,
                "name": str(item.get(k_name, "")).strip(),
                "publish_date": cls._roc_any_to_western(
                    item.get(k_pub, "")) or "",
                "disposal_start": ds or "", "disposal_end": de or "",
                "condition": str(item.get(k_cond, "")).strip(),
                "measure": str(item.get(k_meas, "")).strip(),
                "is_active": bool(ds and de and ds <= today_str <= de),
            }
            if code not in seen or rec["disposal_start"] > seen[code]["disposal_start"]:
                seen[code] = rec
        return list(seen.values())

    TPEX_DISPOSAL_URL = ("https://www.tpex.org.tw/openapi/v1/"
                         "tpex_disposal_information")

    @classmethod
    def _fetch_tpex_disposal(cls) -> list:
        """上櫃處置股 (櫃買中心 OpenAPI, key 名稱容錯解析)"""
        r = requests.get(cls.TPEX_DISPOSAL_URL,
                         headers={**cls.HEADERS,
                                  'Accept': 'application/json'},
                         timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"TPEX HTTP {r.status_code}")
        arr = r.json()
        if not isinstance(arr, list) or not arr:
            return []
        today_str = datetime.now().strftime("%Y-%m-%d")
        s = arr[0]
        k_code = cls._resolve_key(s, "SecuritiesCompanyCode", "Code", "代號")
        k_name = cls._resolve_key(s, "CompanyName", "Name", "名稱")
        k_period = cls._resolve_key(s, "DispositionPeriod", "DisposalPeriod",
                                    "Period", "起訖", "期間")
        k_pub = cls._resolve_key(s, "Date", "公布", "日期")
        k_cond = cls._resolve_key(s, "Condition", "Criteria", "Reason", "條件")
        # ★ 修 bug: "Disposition" 會撞到 DispositionPeriod, 需排除含 Period 的 key
        k_meas = None
        for k in s.keys():
            ks = str(k)
            if "Period" in ks or "期間" in ks or "起訖" in ks:
                continue   # 跳過期間欄位
            if any(x in ks for x in ("Measure", "措施", "Method", "處置方式")):
                k_meas = k
                break
        if k_meas is None:
            # 次選: 含 Disposition 但不含 Period 的欄位
            for k in s.keys():
                ks = str(k)
                if "Disposition" in ks and "Period" not in ks:
                    k_meas = k
                    break
        if not (k_code and k_period):
            raise RuntimeError(f"TPEX 欄位無法辨識: {list(s.keys())}")
        seen = {}
        for item in arr:
            code = str(item.get(k_code, "")).strip()
            if not cls._is_common_stock(code):
                continue
            ds, de = cls._parse_period(item.get(k_period, ""))
            rec = {
                "code": code,
                "name": str(item.get(k_name, "")).strip(),
                "publish_date": cls._roc_any_to_western(
                    item.get(k_pub, "")) or "",
                "disposal_start": ds or "", "disposal_end": de or "",
                "condition": str(item.get(k_cond, "")).strip(),
                "measure": str(item.get(k_meas, "")).strip(),
                "is_active": bool(ds and de and ds <= today_str <= de),
                "market": "TWO",
            }
            if code not in seen or rec["disposal_start"] > seen[code]["disposal_start"]:
                seen[code] = rec
        return list(seen.values())

    @classmethod
    def fetch_disposal_list(cls, days_back: int = 30) -> list:
        """上市 (主要API+OpenAPI備援) + 上櫃 (TPEX OpenAPI) 全部處置股"""
        cls._last_error = ""
        err1 = err2 = err3 = ""
        twse_recs = []
        try:
            twse_recs = cls._fetch_via_website(days_back)
            if not twse_recs:
                err1 = "回傳 0 筆"
        except Exception as e:
            err1 = str(e)
        if not twse_recs:
            try:
                twse_recs = cls._fetch_via_openapi()
            except Exception as e:
                err2 = str(e)
        for r in twse_recs:
            r.setdefault("market", "TW")
        # 上櫃 (失敗不影響上市結果)
        tpex_recs = []
        try:
            tpex_recs = cls._fetch_tpex_disposal()
        except Exception as e:
            err3 = str(e)
        all_recs = twse_recs + tpex_recs
        if all_recs:
            if err3:
                cls._last_error = f"⚠️ 上櫃處置抓取失敗(僅顯示上市): {err3}"
            return all_recs
        cls._last_error = (f"上市主API: {err1}  |  上市OpenAPI: {err2}  |  "
                           f"上櫃: {err3}")
        return []


def _fetch_twse_stock_day(code: str, months_back: int = 2) -> pd.DataFrame:
    """
    用證交所 STOCK_DAY API 抓上市個股日 OHLCV (繞開 Yahoo SSL 問題)。
    一次一個月, 抓最近 months_back 個月拼起來。
    回傳 DataFrame[index=date, Open/High/Low/Close/Volume] 或空。
    """
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    all_rows = []
    now = datetime.now()
    for m in range(months_back):
        y = now.year
        mo = now.month - m
        while mo <= 0:
            mo += 12
            y -= 1
        date_str = f"{y}{mo:02d}01"
        url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
        try:
            r = requests.get(url, params={
                "response": "json", "date": date_str, "stockNo": code,
            }, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            payload = r.json()
            if payload.get("stat") != "OK":
                continue
            fields = payload.get("fields", [])
            def _fi(name, default):
                try:
                    return fields.index(name)
                except ValueError:
                    return default
            i_date = _fi("日期", 0)
            i_open = _fi("開盤價", 3)
            i_high = _fi("最高價", 4)
            i_low = _fi("最低價", 5)
            i_close = _fi("收盤價", 6)
            i_vol = _fi("成交股數", 1)

            def _num(v):
                s = str(v).replace(",", "").strip()
                try:
                    return float(s)
                except ValueError:
                    return None

            for row in payload.get("data", []):
                try:
                    roc = str(row[i_date]).strip()  # 民國 115/07/08
                    parts = roc.split("/")
                    west = datetime(int(parts[0]) + 1911,
                                    int(parts[1]), int(parts[2]))
                    o = _num(row[i_open])
                    h = _num(row[i_high])
                    lo = _num(row[i_low])
                    c = _num(row[i_close])
                    v = _num(row[i_vol]) or 0
                    # 開高低收任一缺就跳過這天 (避免 OHLC 不齊)
                    if None in (o, h, lo, c):
                        continue
                    all_rows.append((west, o, h, lo, c, v))
                except (ValueError, IndexError):
                    continue
        except Exception:
            continue

    if not all_rows:
        return pd.DataFrame()
    all_rows.sort(key=lambda x: x[0])
    df = pd.DataFrame({
        "Open":  [r[1] for r in all_rows],
        "High":  [r[2] for r in all_rows],
        "Low":   [r[3] for r in all_rows],
        "Close": [r[4] for r in all_rows],
        "Volume":[r[5] for r in all_rows],
    }, index=pd.DatetimeIndex([r[0] for r in all_rows]))
    df = df[~df.index.duplicated(keep="last")]
    return df


def check_disposal_ma20(code, name, disposal):
    market = disposal.get('market', 'TW')
    df = None

    # 上市: 優先用證交所 STOCK_DAY (繞開 Yahoo SSL)
    if market == "TW":
        twse_df = _fetch_twse_stock_day(code, months_back=2)
        if not twse_df.empty and len(twse_df) >= 20:
            df = twse_df

    # 上櫃 或 證交所抓取失敗: 用 yfinance 備援
    if df is None:
        ticker = f"{code}.{market}"
        yf_df = StockDataFetcher.fetch_history(ticker, period="3mo")
        if not yf_df.empty and len(yf_df) >= 20:
            df = yf_df

    if df is None or df.empty or len(df) < 20:
        return None

    df = df.copy()
    df["MA20"] = df["Close"].rolling(20).mean()
    last = df.iloc[-1]
    close = float(last["Close"])
    ma20 = float(last["MA20"]) if not pd.isna(last["MA20"]) else None
    if not ma20 or ma20 <= 0:
        return None
    diff = (close - ma20) / ma20 * 100
    ad = abs(diff)
    chg5 = (close / float(df["Close"].iloc[-6]) - 1) * 100 if len(df) >= 6 else 0
    return {
        "code": code, "name": name,
        "close": round(close, 2), "ma20": round(ma20, 2),
        "diff_pct": round(diff, 2), "abs_diff_pct": round(ad, 2),
        "color": "🔴" if ad <= 2 else "🟡" if ad <= 5 else "⚪",
        "change_5d_pct": round(chg5, 2),
        "disposal": disposal,
    }


def scan_disposal_ma20(days_back=30, only_active=True, progress_cb=None):
    dlist = DisposalStockFetcher.fetch_disposal_list(days_back)
    if only_active:
        dlist = [d for d in dlist if d["is_active"]]
    results = []
    total = len(dlist)
    for i, d in enumerate(dlist, 1):
        if progress_cb:
            progress_cb(i, total, f"{d['code']} {d['name']}")
        info = check_disposal_ma20(d["code"], d["name"], d)
        if info:
            results.append(info)
    results.sort(key=lambda x: x["abs_diff_pct"])
    return results


# ==================================================================
#   判斷「證交所今天有沒有開盤/更新資料」 (給排程器用)
# ==================================================================
def is_market_open_today() -> tuple[bool, str]:
    """
    檢查證交所「今天日期」有沒有大盤行情資料。
    有 → 今天有開盤且資料已更新 (可推播)
    沒有 → 假日/颱風假/尚未更新 (應延後或不推)

    回傳 (是否就緒, 說明訊息)。
    用 MI_INDEX 帶今天日期查詢, stat=OK 且有資料表示已就緒。
    """
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    today = datetime.now()
    # 週末直接判定沒開盤 (不用打 API)
    if today.weekday() >= 5:
        return False, "週末休市"

    date_str = today.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
    try:
        r = requests.get(url, params={
            "response": "json", "date": date_str, "type": "ALLBUT0999",
        }, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} (證交所暫時無回應)"
        payload = r.json()
        if payload.get("stat") != "OK":
            # stat 不 OK = 今天沒有資料 (假日/颱風/尚未更新)
            return False, f"今日尚無行情資料 (stat={payload.get('stat','?')})"
        # 有資料表 = 確實有開盤且已更新
        for t in payload.get("tables", []):
            if t.get("data"):
                return True, "今日行情已更新"
        # 舊格式
        for i in range(9, 0, -1):
            if payload.get(f"data{i}"):
                return True, "今日行情已更新"
        return False, "回應無資料表 (可能尚未更新)"
    except Exception as e:
        return False, f"檢查失敗: {e}"


# 台灣證交所固定國定假日休市 (每年更新; 颱風臨時假靠 is_market_open_today 動態判斷)
# 格式 "MM-DD" (不分年份的固定假日) — 農曆假日每年不同, 用動態判斷補足
TW_FIXED_HOLIDAYS = {
    "01-01",  # 元旦
    "02-28",  # 和平紀念日
    "04-04",  # 兒童節
    "04-05",  # 清明節 (約)
    "05-01",  # 勞動節
    "10-10",  # 國慶日
}


def is_fixed_holiday(dt: datetime = None) -> bool:
    """是否為固定國定假日 (快速判斷, 不用打 API)。農曆假日與颱風假靠 is_market_open_today。"""
    dt = dt or datetime.now()
    return dt.strftime("%m-%d") in TW_FIXED_HOLIDAYS


# ==================================================================
#   取得個股「歷史處置期間」清單 (給 K 線圖畫網底用)
# ==================================================================
_disposal_history_cache = {}


def get_disposal_periods(code: str, days_back: int = 200) -> list[dict]:
    """
    取得某檔股票在過去 days_back 天內「所有」處置期間 (含已結束的)。
    用於在 K 線圖上畫出每一段處置期間的網底。

    與 fetch_disposal_list 的差異:
      • fetch_disposal_list: 只留最新一筆 (判斷「目前是否處置中」)
      • 這裡: 保留同一檔的「所有」處置紀錄 (歷史多次處置都要)

    回傳: [{start, end, measure}, ...] 依 start 排序
    """
    code = str(code).strip()
    cache_key = f"{code}_{days_back}_{datetime.now().strftime('%Y-%m-%d')}"
    if cache_key in _disposal_history_cache:
        return _disposal_history_cache[cache_key]

    D = DisposalStockFetcher
    periods = []
    end = datetime.now()
    start = end - timedelta(days=days_back)

    # ---- 上市: 證交所公告 (帶日期範圍) ----
    try:
        r = requests.get(D.API_URL, params={
            "response": "json",
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
        }, headers=D.HEADERS, timeout=15)
        if r.status_code == 200:
            payload = r.json()
            if payload.get("stat") == "OK":
                for row in payload.get("data", []):
                    if len(row) < 9:
                        continue
                    if str(row[2]).strip() != code:
                        continue
                    ds, de = D._parse_period(str(row[6]))
                    if ds and de:
                        periods.append({"start": ds, "end": de,
                                        "measure": str(row[7]).strip()})
    except Exception as e:
        print(f"[disposal_hist] 上市查詢失敗 {code}: {e}")

    # ---- 上櫃: TPEX OpenAPI (只有現行, 沒有歷史範圍參數) ----
    if not periods:
        try:
            tpex = D._fetch_tpex_disposal()
            for rec in tpex:
                if rec["code"] == code and rec["disposal_start"]:
                    periods.append({"start": rec["disposal_start"],
                                    "end": rec["disposal_end"],
                                    "measure": rec.get("measure", "")})
        except Exception:
            pass

    # 去重 + 排序
    seen = set()
    uniq = []
    for p in sorted(periods, key=lambda x: x["start"]):
        key = (p["start"], p["end"])
        if key not in seen:
            seen.add(key)
            uniq.append(p)

    _disposal_history_cache[cache_key] = uniq
    return uniq
