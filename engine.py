# ============================================================
#  QuestLife Signal Bot — engine.py  v5.0 STRICT FINAL
#  13-Pillar | 6 Hard Filters | Ban Detection | Max 5 Signals
# ============================================================

import asyncio
import logging
import os
import re
import time

import pandas as pd
import ccxt.async_support as ccxt

from ta.momentum   import RSIIndicator
from ta.trend      import EMAIndicator, ADXIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume     import OnBalanceVolumeIndicator

from market_intel import MarketContext, build_market_context

logger = logging.getLogger(__name__)

# ─── Thresholds ───────────────────────────────────────────────────────────────
MIN_24H_VOLUME_USDT = 10_000_000
MAX_FUNDING_RATE    = 0.0015
MIN_ADX             = 22
MANUAL_THRESHOLD    = 75
AUTO_THRESHOLD      = 85
MAX_SIGNALS         = 5
MAX_LONGS           = 3
MAX_SHORTS          = 2

# ─── Ban Detection ────────────────────────────────────────────────────────────
BAN_FILE = "/data/ban_until.txt"

def is_banned() -> bool:
    """Check if Binance IP ban is currently active."""
    if not os.path.exists(BAN_FILE):
        return False
    try:
        with open(BAN_FILE) as f:
            ban_ts = float(f.read().strip())
        if time.time() < ban_ts:
            return True
        else:
            os.remove(BAN_FILE)  # Ban expired, clean up
            return False
    except Exception:
        return False

def get_ban_remaining_mins() -> int:
    """Return minutes remaining on ban."""
    try:
        with open(BAN_FILE) as f:
            ban_ts = float(f.read().strip())
        return max(0, int((ban_ts - time.time()) / 60))
    except Exception:
        return 0

def save_ban(banned_until_ms: int):
    """Save ban expiry timestamp to disk — survives restarts."""
    try:
        os.makedirs("/data", exist_ok=True)
        ban_ts  = banned_until_ms / 1000
        mins    = int((ban_ts - time.time()) / 60)
        with open(BAN_FILE, "w") as f:
            f.write(str(ban_ts))
        logger.error(f"Binance 418 ban saved — expires in {mins} minutes. All scans paused automatically.")
    except Exception as e:
        logger.error(f"Could not save ban file: {e}")


# ─── OHLCV ────────────────────────────────────────────────────────────────────

async def fetch_ohlcv_safe(exchange, symbol, timeframe, limit):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 50:
            return None
        df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","vol"])
        return df.dropna()
    except Exception as e:
        logger.debug(f"OHLCV [{symbol} {timeframe}]: {e}")
        return None


# ─── Indicators ───────────────────────────────────────────────────────────────

def add_indicators(df):
    df    = df.copy()
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["vol"]

    df["EMA_20"]   = EMAIndicator(close=close, window=20).ema_indicator()
    df["EMA_50"]   = EMAIndicator(close=close, window=50).ema_indicator()
    df["EMA_200"]  = EMAIndicator(close=close, window=200).ema_indicator()
    df["RSI_14"]   = RSIIndicator(close=close, window=14).rsi()

    _macd          = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD"]     = _macd.macd()
    df["MACD_SIG"] = _macd.macd_signal()
    df["MACD_HIST"]= _macd.macd_diff()

    df["ADX_14"]   = ADXIndicator(high=high, low=low, close=close, window=14).adx()
    df["ATR_14"]   = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()

    _bb            = BollingerBands(close=close, window=20, window_dev=2)
    df["BB_UP"]    = _bb.bollinger_hband()
    df["BB_MID"]   = _bb.bollinger_mavg()
    df["BB_LOW"]   = _bb.bollinger_lband()
    df["BB_WIDTH"] = (df["BB_UP"] - df["BB_LOW"]) / df["BB_MID"].replace(0, float("nan"))

    df["OBV"]      = OnBalanceVolumeIndicator(close=close, volume=vol).on_balance_volume()
    df["OBV_EMA"]  = EMAIndicator(close=df["OBV"], window=20).ema_indicator()
    df["VOL_MA20"] = vol.rolling(window=20).mean()

    return df


# ─── Support & Resistance ─────────────────────────────────────────────────────

def find_sr_levels(df, n=3):
    tail = df.tail(60).reset_index(drop=True)
    res, sup = [], []
    for i in range(n, len(tail) - n):
        if all(tail["high"].iloc[i] >= tail["high"].iloc[i-j] for j in range(1, n+1)) and \
           all(tail["high"].iloc[i] >= tail["high"].iloc[i+j] for j in range(1, n+1)):
            res.append(tail["high"].iloc[i])
        if all(tail["low"].iloc[i] <= tail["low"].iloc[i-j] for j in range(1, n+1)) and \
           all(tail["low"].iloc[i] <= tail["low"].iloc[i+j] for j in range(1, n+1)):
            sup.append(tail["low"].iloc[i])
    return sorted(set(res)), sorted(set(sup))


def check_sr(entry, tp, sl, direction, res, sup):
    tol = 0.015
    if direction == "LONG":
        blockers = [r for r in res if entry * 1.005 < r < tp]
        if blockers:
            bp = (min(blockers) - entry) / (tp - entry + 1e-10)
            if bp < 0.4:   return -15, f"HardBlock@{min(blockers):.4g}"
            elif bp < 0.7: return -8,  f"SoftBlock@{min(blockers):.4g}"
            else:          return -3,  f"FarBlock@{min(blockers):.4g}"
        if any(abs(entry - s) / entry < tol for s in sup): return 10, "AtSupport"
        return 5, "PathClear"
    else:
        blockers = [s for s in sup if tp < s < entry * 0.995]
        if blockers:
            bp = (entry - max(blockers)) / (entry - tp + 1e-10)
            if bp < 0.4:   return -15, f"HardBlock@{max(blockers):.4g}"
            elif bp < 0.7: return -8,  f"SoftBlock@{max(blockers):.4g}"
            else:          return -3,  f"FarBlock@{max(blockers):.4g}"
        if any(abs(entry - r) / entry < tol for r in res): return 10, "AtResistance"
        return 5, "PathClear"


# ─── Fibonacci ────────────────────────────────────────────────────────────────

def get_fib_levels(df, lookback=50):
    recent = df.tail(lookback)
    hi     = recent["high"].max()
    lo     = recent["low"].min()
    diff   = hi - lo
    return {
        "high":  hi, "low": lo,
        "0.236": hi - diff * 0.236,
        "0.382": hi - diff * 0.382,
        "0.500": hi - diff * 0.500,
        "0.618": hi - diff * 0.618,
        "0.786": hi - diff * 0.786,
    }


def check_fib(entry, fibs, direction):
    tol = entry * 0.015
    if direction == "LONG":
        if abs(entry - fibs["0.618"]) < tol or abs(entry - fibs["0.786"]) < tol: return 10, "GoldenPocket"
        if abs(entry - fibs["0.500"]) < tol: return 6, "Fib50"
        if abs(entry - fibs["0.382"]) < tol: return 4, "Fib38"
    else:
        if abs(entry - fibs["0.236"]) < tol or abs(entry - fibs["0.382"]) < tol: return 10, "FibResist"
        if abs(entry - fibs["0.500"]) < tol: return 6, "Fib50"
    return 0, ""


# ─── Candle Pattern ───────────────────────────────────────────────────────────

def detect_candle(df, direction):
    if len(df) < 3: return 0, ""
    last = df.iloc[-1]; prev = df.iloc[-2]
    body  = abs(last["close"] - last["open"])
    rng   = last["high"] - last["low"]
    uw    = last["high"] - max(last["close"], last["open"])
    lw    = min(last["close"], last["open"]) - last["low"]
    if rng < 1e-10: return 0, ""
    br    = body / rng
    bull  = last["close"] > last["open"]
    bear  = last["close"] < last["open"]

    if br < 0.08: return -8, "Doji"
    if bull and prev["close"] < prev["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]:
        return (10,"BullEngulf") if direction == "LONG" else (-10,"BullEngulf-Contra")
    if bear and prev["close"] > prev["open"] and last["close"] < prev["open"] and last["open"] > prev["close"]:
        return (10,"BearEngulf") if direction == "SHORT" else (-10,"BearEngulf-Contra")
    if lw > body * 2 and uw < body * 0.5 and br > 0.1:
        return (8,"Hammer") if direction == "LONG" else (-6,"Hammer-Contra")
    if uw > body * 2 and lw < body * 0.5 and br > 0.1:
        return (8,"ShootingStar") if direction == "SHORT" else (-6,"ShootingStar-Contra")
    if br > 0.65:
        if bull and direction == "LONG":  return 5, "StrongBull"
        if bear and direction == "SHORT": return 5, "StrongBear"
        if bull and direction == "SHORT": return -5, "BullContra"
        if bear and direction == "LONG":  return -5, "BearContra"
    return 0, "Neutral"


# ─── BB Squeeze ───────────────────────────────────────────────────────────────

def check_bb_squeeze(df, direction):
    if len(df) < 30: return 0, ""
    last = df.iloc[-1]
    bw   = last["BB_WIDTH"]
    if pd.isna(bw): return 0, ""
    bwmin = df["BB_WIDTH"].tail(50).min()
    bwmax = df["BB_WIDTH"].tail(50).max()
    if bwmax == bwmin: return 0, ""
    sq = (bw - bwmin) / (bwmax - bwmin)
    if sq < 0.20:
        if direction == "LONG"  and last["close"] > last["BB_MID"]: return 10, "BBSqueeze↑"
        if direction == "SHORT" and last["close"] < last["BB_MID"]: return 10, "BBSqueeze↓"
        return 5, "BBSqueeze"
    if sq < 0.40: return 3, "BBCompress"
    return 0, ""


# ─── OBV ──────────────────────────────────────────────────────────────────────

def check_obv(df, direction):
    if len(df) < 5: return 0, ""
    last = df.iloc[-1]
    obv, obv_ema, obv_prev = last["OBV"], last["OBV_EMA"], df["OBV"].iloc[-5]
    if pd.isna(obv_ema): return 0, ""
    up   = obv > obv_ema and obv > obv_prev
    down = obv < obv_ema and obv < obv_prev
    if direction == "LONG":
        if up:   return 10, "OBV↑OK"
        if down: return -8, "OBV↓Warn"
    else:
        if down: return 10, "OBV↓OK"
        if up:   return -8, "OBV↑Warn"
    return 0, ""


# ─── Multiple TP ──────────────────────────────────────────────────────────────

def calc_tps(entry, atr, direction, res, sup):
    sl_d = atr * 1.5
    if direction == "LONG":
        tp1 = entry + sl_d * 1.0
        tp2 = entry + sl_d * 2.0
        tp3 = entry + sl_d * 3.0
        nearby = [r for r in res if tp1 < r < tp3 * 1.05]
        if nearby:
            tp2 = min(nearby)
            tp3 = max(nearby) if len(nearby) > 1 else tp3
    else:
        tp1 = entry - sl_d * 1.0
        tp2 = entry - sl_d * 2.0
        tp3 = entry - sl_d * 3.0
        nearby = [s for s in sup if tp3 * 0.95 < s < tp1]
        if nearby:
            tp2 = max(nearby)
            tp3 = min(nearby) if len(nearby) > 1 else tp3
    return round(tp1, 8), round(tp2, 8), round(tp3, 8)


# ─── Correlation Filter ────────────────────────────────────────────────────────

def filter_correlated(signals):
    final, longs, shorts = [], 0, 0
    for sig in signals:
        is_long = "LONG" in sig["dir"]
        if is_long:
            if longs >= MAX_LONGS: continue
            longs += 1
        else:
            if shorts >= MAX_SHORTS: continue
            shorts += 1
        final.append(sig)
        if len(final) >= MAX_SIGNALS: break
    return final


# ─── Per-Symbol Analysis ──────────────────────────────────────────────────────

async def analyze_symbol(exchange, symbol, ticker, funding_rate, ctx):
    coin      = symbol.replace("/USDT:USDT","").replace("/USDT","")
    quote_vol = ticker.get("quoteVolume") or 0

    if quote_vol < MIN_24H_VOLUME_USDT: return None
    if funding_rate is not None and abs(funding_rate) > MAX_FUNDING_RATE: return None

    df_1h, df_4h = await asyncio.gather(
        fetch_ohlcv_safe(exchange, symbol, "1h", 210),
        fetch_ohlcv_safe(exchange, symbol, "4h", 100),
    )
    if df_1h is None or df_4h is None: return None

    df_1h = add_indicators(df_1h).dropna()
    df_4h = add_indicators(df_4h).dropna()
    if len(df_1h) < 5 or len(df_4h) < 3: return None

    last  = df_1h.iloc[-1]
    l4h   = df_4h.iloc[-1]
    entry = last["close"]
    adx   = last["ADX_14"]
    rsi   = last["RSI_14"]
    atr   = last["ATR_14"]

    if pd.isna(atr) or atr == 0 or atr < entry * 0.0001: return None

    score = 0; direction = None; reasons = []

    # Pillar 1: EMA 20/50 (20pts)
    if last["EMA_20"] > last["EMA_50"]:   score += 20; direction = "LONG";  reasons.append("EMA↑")
    elif last["EMA_20"] < last["EMA_50"]: score += 20; direction = "SHORT"; reasons.append("EMA↓")
    else: return None

    # BTC Gate
    if coin != "BTC" and direction == "LONG" and ctx.btc_is_bearish(): return None

    # ADX filter — direction aware
    # SHORTs allowed at ADX 18+ (downtrends have naturally lower ADX)
    # LONGs require ADX 22+ (need stronger trend to go long)
    adx_min = 18 if direction == "SHORT" else MIN_ADX
    if adx < adx_min: return None

    # Pillar 2: EMA 200 (10pts)
    ema200 = last["EMA_200"]
    if pd.notna(ema200):
        if direction == "LONG"  and entry > ema200:  score += 10; reasons.append("AboveEMA200")
        elif direction == "SHORT" and entry < ema200: score += 10; reasons.append("BelowEMA200")
        elif direction == "LONG"  and entry < ema200: score -= 5;  reasons.append("BelowEMA200!")
        elif direction == "SHORT" and entry > ema200: score -= 5;  reasons.append("AboveEMA200!")

    # Hard Filter: 4H MTF must confirm
    if direction == "LONG"  and not (l4h["EMA_20"] > l4h["EMA_50"]): return None
    if direction == "SHORT" and not (l4h["EMA_20"] < l4h["EMA_50"]): return None
    score += 15; reasons.append("4H✓")

    # Hard Filter: No chasing
    if direction == "LONG"  and rsi > 68: return None
    if direction == "SHORT" and rsi < 32: return None

    # Pillar 3: MACD (10pts)
    macd      = last["MACD"]
    macd_sig  = last["MACD_SIG"]
    macd_hist = last["MACD_HIST"]
    prev_hist = df_1h["MACD_HIST"].iloc[-2] if len(df_1h) > 2 else 0
    if pd.notna(macd) and pd.notna(macd_sig):
        if direction == "LONG":
            if macd > macd_sig and pd.notna(macd_hist) and macd_hist > 0 and macd_hist > prev_hist: score += 10; reasons.append("MACD↑")
            elif macd > macd_sig: score += 5; reasons.append("MACD+")
            elif macd < macd_sig: score -= 3; reasons.append("MACD-")
        else:
            if macd < macd_sig and pd.notna(macd_hist) and macd_hist < 0 and macd_hist < prev_hist: score += 10; reasons.append("MACD↓")
            elif macd < macd_sig: score += 5; reasons.append("MACD-")
            elif macd > macd_sig: score -= 3; reasons.append("MACD+Warn")

    # Pillar 4: RSI (20pts)
    if direction == "LONG":
        if 35 <= rsi <= 52:  score += 20; reasons.append(f"RSI({rsi:.0f})")
        elif rsi < 35:       score += 10; reasons.append(f"RSI-OS({rsi:.0f})")
        elif rsi <= 62:      score += 6;  reasons.append(f"RSI-M({rsi:.0f})")
    else:
        if 48 <= rsi <= 65:  score += 20; reasons.append(f"RSI({rsi:.0f})")
        elif rsi > 65:       score += 10; reasons.append(f"RSI-OB({rsi:.0f})")
        elif rsi >= 38:      score += 6;  reasons.append(f"RSI-M({rsi:.0f})")

    # Pillar 5: ADX (10pts)
    if adx >= 35:   score += 10; reasons.append(f"ADX-S({adx:.0f})")
    elif adx >= 28: score += 7;  reasons.append(f"ADX-G({adx:.0f})")
    elif adx >= 22: score += 3;  reasons.append(f"ADX-OK({adx:.0f})")

    # Pillar 6: BB Squeeze (10pts)
    bb_pts, bb_r = check_bb_squeeze(df_1h, direction)
    score += bb_pts
    if bb_r: reasons.append(bb_r)

    # Pillar 7: OBV (10pts)
    obv_pts, obv_r = check_obv(df_1h, direction)
    score += obv_pts
    if obv_r: reasons.append(obv_r)

    # Volume bar
    vol_ma = last["VOL_MA20"]
    if pd.notna(vol_ma) and vol_ma > 0:
        vr = last["vol"] / vol_ma
        if vr >= 1.5:   score += 5; reasons.append(f"Vol({vr:.1f}x)")
        elif vr >= 1.0: score += 2

    # Pillar 8: Funding + OI + L/S (10pts)
    fp = 0
    if funding_rate is not None:
        fr = funding_rate
        if direction == "LONG":
            if fr < -0.0001:        fp += 6; reasons.append(f"FR+({fr:.3%})")
            elif abs(fr) <= 0.0005: fp += 3
        else:
            if fr > 0.0001:         fp += 6; reasons.append(f"FR-({fr:.3%})")
            elif abs(fr) <= 0.0005: fp += 3
    if ctx.oi_change_pct > 1.0:    fp += 3; reasons.append(f"OI↑")
    elif ctx.oi_change_pct > 0:    fp += 1
    elif ctx.oi_change_pct < -1.0: fp -= 3; reasons.append("OI↓")
    ls = ctx.ls_ratio
    if direction == "LONG"  and ls < 0.8:  fp += 2; reasons.append(f"L/S({ls:.2f})")
    if direction == "SHORT" and ls > 1.5:  fp += 2; reasons.append(f"L/S({ls:.2f})")
    score += min(fp, 10)

    # Pillar 9: F&G (5pts)
    fg = ctx.fear_greed
    if direction == "LONG":
        if fg < 25:   score += 5; reasons.append(f"Fear({fg})")
        elif fg < 50: score += 2
        elif fg > 75: score -= 5; reasons.append(f"Greed!({fg})")
    else:
        if fg > 75:   score += 5; reasons.append(f"Greed({fg})")
        elif fg >= 50:score += 2
        elif fg < 25: score -= 5; reasons.append(f"Fear!({fg})")

    # Pillar 10: News (8pts)
    news = ctx.news_sentiment.get(coin, "NEUTRAL")
    if news == "POSITIVE" and direction == "LONG":    score += 8; reasons.append("News+")
    elif news == "POSITIVE" and direction == "SHORT":  score -= 5
    elif news == "NEGATIVE" and direction == "LONG":
        score -= 10; reasons.append("News-!")
        if score < 55: return None
    elif news == "NEGATIVE" and direction == "SHORT":  score += 8; reasons.append("News-Short")

    # Macro penalty
    if ctx.macro_event_today:
        pen = 20 if ctx.macro_event_impact == "HIGH" else 10
        score -= pen; reasons.append(f"Macro-{pen}")
    if direction == "LONG" and coin != "BTC" and ctx.btc_dominance > 55:
        score -= 5; reasons.append(f"Dom>{ctx.btc_dominance:.0f}%")

    score = max(0, score)
    if score < MANUAL_THRESHOLD - 20: return None

    # TP/SL
    if direction == "LONG":
        sl = entry - atr * 1.5; icon = "🟢"
    else:
        sl = entry + atr * 1.5; icon = "🔴"

    if abs(sl - entry) < entry * 0.0001: return None

    tp_main = entry + atr * 3.0 if direction == "LONG" else entry - atr * 3.0

    # Pillar 11: S/R + Fibonacci (10pts)
    res, sup       = find_sr_levels(df_1h)
    fibs           = get_fib_levels(df_1h)
    sr_pts, sr_r   = check_sr(entry, tp_main, sl, direction, res, sup)
    fib_pts, fib_r = check_fib(entry, fibs, direction)
    score += sr_pts + min(fib_pts, 10)
    if sr_r:  reasons.append(sr_r)
    if fib_r: reasons.append(fib_r)

    # Pillar 12: Candle Pattern (10pts)
    cp, cr = detect_candle(df_1h, direction)
    score += cp
    if cr: reasons.append(cr)

    score = max(0, score)
    if score < MANUAL_THRESHOLD: return None

    tp1, tp2, tp3 = calc_tps(entry, atr, direction, res, sup)
    sl_pct = abs(entry - sl) / entry
    lev    = min(20, max(1, round(0.02 / sl_pct))) if sl_pct > 0 else 1
    rr     = round(abs(tp2 - entry) / abs(sl - entry), 2)
    liq_est = entry * 0.92 if direction == "LONG" else entry * 1.08

    return {
        "symbol"       : symbol.replace(":USDT",""),
        "score"        : score,
        "dir"          : f"{icon} {direction}",
        "entry"        : entry,
        "tp1"          : tp1,
        "tp2"          : tp2,
        "tp3"          : tp3,
        "sl"           : sl,
        "lev"          : lev,
        "rsi"          : round(rsi, 1),
        "adx"          : round(adx, 1),
        "rr"           : rr,
        "atr"          : atr,
        "funding_rate" : round(funding_rate * 100, 4) if funding_rate is not None else None,
        "vol_24h_m"    : round(quote_vol / 1_000_000, 1),
        "news"         : news,
        "news_headline": ctx.news_headlines.get(coin, ""),
        "liq_est"      : round(liq_est, 6),
        "sl_pct"       : round(sl_pct * 100, 2),
        "reasons"      : " | ".join(reasons),
    }


# ─── Main Entry Point ─────────────────────────────────────────────────────────

async def get_top_signals():
    """
    Full pipeline with ban detection.
    Returns (signals, context).
    If banned: returns ([], empty_context) immediately without hitting Binance.
    """

    # ── Ban check FIRST — before any Binance call ─────────────────────────
    if is_banned():
        mins = get_ban_remaining_mins()
        logger.warning(f"Binance ban active — {mins}min remaining. Skipping scan.")
        return [], MarketContext()

    token    = os.getenv("CRYPTOPANIC_TOKEN", "")
    exchange = ccxt.binance({
        "options"        : {"defaultType": "future"},
        "enableRateLimit": True,
    })

    try:
        markets     = await exchange.load_markets()
        all_futures = [s for s in markets if s.endswith("/USDT:USDT")]

        logger.info("Fetching tickers (top 100, Binance 418 safe)...")
        try:
            tickers = await exchange.fetch_tickers(all_futures[:100])
        except Exception:
            tickers = {}

        liquid      = [s for s in tickers if (tickers[s].get("quoteVolume") or 0) >= MIN_24H_VOLUME_USDT]
        sorted_syms = sorted(liquid, key=lambda s: tickers[s].get("quoteVolume") or 0, reverse=True)[:25]
        logger.info(f"Scanning {len(sorted_syms)} pairs | Top5: {[s.replace('/USDT:USDT','') for s in sorted_syms[:5]]}")

        funding_map = {}
        try:
            fd = await exchange.fetch_funding_rates(sorted_syms)
            for sym, d in fd.items():
                funding_map[sym] = d.get("fundingRate")
        except Exception as e:
            logger.warning(f"Funding rates failed: {e}")

        ctx = await build_market_context(exchange, sorted_syms, token)

        raw = []
        for sym in sorted_syms:
            r = await analyze_symbol(
                exchange, sym,
                tickers.get(sym, {}),
                funding_map.get(sym),
                ctx
            )
            if r:
                raw.append(r)
                logger.info(f"PASS {r['symbol']:15s} {r['dir']} Score:{r['score']} | {r['reasons'][:55]}")
            await asyncio.sleep(0.8)  # Binance 418 protection

        raw.sort(key=lambda x: x["score"], reverse=True)
        final = filter_correlated(raw)
        logger.info(f"Done. Raw:{len(raw)} Final:{len(final)}")
        return final, ctx

    except Exception as e:
        err = str(e)
        # ── Detect and save ban ───────────────────────────────────────────
        if "418" in err or "banned until" in err.lower():
            match = re.search(r"banned until (\d+)", err)
            if match:
                save_ban(int(match.group(1)))
            else:
                # No timestamp found — set 2 hour default ban
                save_ban(int((time.time() + 7200) * 1000))
        else:
            logger.error(f"get_top_signals error: {e}", exc_info=True)
        return [], MarketContext()

    finally:
        await exchange.close()
