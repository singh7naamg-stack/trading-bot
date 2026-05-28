# ============================================================
#  QuestLife Signal Bot — engine.py  v6.0 ADAPTIVE
#  Works in BOTH trending AND ranging/slow bleed markets
#  Key changes from v5:
#  - SHORTs: no longer require 4H EMA cross (impossible in slow bleeds)
#            uses price vs 4H EMA50 instead (much more responsive)
#  - LONGs:  added bounce detection for oversold recoveries
#  - Scoring: recalibrated for realistic achievable scores
#  - Threshold: 60% manual / 75% auto
#  - Ban detection: checks disk before ANY Binance call
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
MANUAL_THRESHOLD    = 60    # lowered from 65 — realistic for current market
AUTO_THRESHOLD      = 75    # lowered from 85
MAX_SIGNALS         = 5
MAX_LONGS           = 3
MAX_SHORTS          = 2

# ─── Ban Detection ────────────────────────────────────────────────────────────
BAN_FILE = "/data/ban_until.txt"

def is_banned() -> bool:
    if not os.path.exists(BAN_FILE):
        return False
    try:
        with open(BAN_FILE) as f:
            ban_ts = float(f.read().strip())
        if time.time() < ban_ts:
            return True
        os.remove(BAN_FILE)
        return False
    except Exception:
        return False

def get_ban_remaining_mins() -> int:
    try:
        with open(BAN_FILE) as f:
            ban_ts = float(f.read().strip())
        return max(0, int((ban_ts - time.time()) / 60))
    except Exception:
        return 0

def save_ban(banned_until_ms: int):
    try:
        os.makedirs("/data", exist_ok=True)
        ban_ts = banned_until_ms / 1000
        mins   = int((ban_ts - time.time()) / 60)
        with open(BAN_FILE, "w") as f:
            f.write(str(ban_ts))
        logger.error(f"Binance 418 ban saved — {mins}min. Scans paused.")
    except Exception as e:
        logger.error(f"Could not save ban file: {e}")


# ─── OHLCV ────────────────────────────────────────────────────────────────────

async def fetch_ohlcv_safe(exchange, symbol, timeframe, limit):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 30:
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

    df["EMA_9"]    = EMAIndicator(close=close, window=9).ema_indicator()
    df["EMA_20"]   = EMAIndicator(close=close, window=20).ema_indicator()
    df["EMA_50"]   = EMAIndicator(close=close, window=50).ema_indicator()
    df["EMA_200"]  = EMAIndicator(close=close, window=200).ema_indicator()
    df["RSI_14"]   = RSIIndicator(close=close, window=14).rsi()
    df["RSI_7"]    = RSIIndicator(close=close, window=7).rsi()

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
    df["BB_PCT"]   = _bb.bollinger_pband()
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
        if all(tail["high"].iloc[i] >= tail["high"].iloc[i-j] for j in range(1,n+1)) and \
           all(tail["high"].iloc[i] >= tail["high"].iloc[i+j] for j in range(1,n+1)):
            res.append(tail["high"].iloc[i])
        if all(tail["low"].iloc[i] <= tail["low"].iloc[i-j] for j in range(1,n+1)) and \
           all(tail["low"].iloc[i] <= tail["low"].iloc[i+j] for j in range(1,n+1)):
            sup.append(tail["low"].iloc[i])
    return sorted(set(res)), sorted(set(sup))


# ─── TP Calculator ────────────────────────────────────────────────────────────

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


# ─── Candle Pattern ───────────────────────────────────────────────────────────

def detect_candle(df, direction):
    if len(df) < 3: return 0, ""
    last = df.iloc[-1]; prev = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng  = last["high"] - last["low"]
    uw   = last["high"] - max(last["close"], last["open"])
    lw   = min(last["close"], last["open"]) - last["low"]
    if rng < 1e-10: return 0, ""
    br   = body / rng
    bull = last["close"] > last["open"]
    bear = last["close"] < last["open"]

    if br < 0.08: return -5, "Doji"
    if bull and prev["close"] < prev["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]:
        return (10,"BullEngulf") if direction == "LONG" else (-8,"BullEngulf-Contra")
    if bear and prev["close"] > prev["open"] and last["close"] < prev["open"] and last["open"] > prev["close"]:
        return (10,"BearEngulf") if direction == "SHORT" else (-8,"BearEngulf-Contra")
    if lw > body * 2 and uw < body * 0.5 and br > 0.1:
        return (8,"Hammer") if direction == "LONG" else (-5,"Hammer-Contra")
    if uw > body * 2 and lw < body * 0.5 and br > 0.1:
        return (8,"ShootingStar") if direction == "SHORT" else (-5,"ShootingStar-Contra")
    if br > 0.6:
        if bull and direction == "LONG":  return 4, "StrongBull"
        if bear and direction == "SHORT": return 4, "StrongBear"
    return 0, "Neutral"


# ─── Core Analysis ────────────────────────────────────────────────────────────

async def analyze_symbol(exchange, symbol, ticker, funding_rate, ctx):
    """
    v6.0 ADAPTIVE analysis — works in trending AND ranging markets.

    Key changes:
    SHORT detection:
      - No longer requires 4H EMA20 < EMA50 (impossible in slow bleeds)
      - Instead: price below 4H EMA50 OR 4H EMA50 declining = bearish bias
      - 1H EMA9 < EMA21 = short term momentum confirms

    LONG detection:
      - Added bounce mode: RSI was oversold (< 35) and now recovering
      - Standard mode: BTC 4H bullish + EMA cross
    """
    coin      = symbol.replace("/USDT:USDT","").replace("/USDT","")
    quote_vol = ticker.get("quoteVolume") or 0

    if quote_vol < MIN_24H_VOLUME_USDT:
        return None
    if funding_rate is not None and abs(funding_rate) > MAX_FUNDING_RATE:
        return None

    # Fetch 1H and 4H
    df_1h, df_4h = await asyncio.gather(
        fetch_ohlcv_safe(exchange, symbol, "1h", 210),
        fetch_ohlcv_safe(exchange, symbol, "4h", 100),
    )
    if df_1h is None or df_4h is None:
        return None

    df_1h = add_indicators(df_1h).dropna()
    df_4h = add_indicators(df_4h).dropna()
    if len(df_1h) < 5 or len(df_4h) < 5:
        return None

    last   = df_1h.iloc[-1]
    prev   = df_1h.iloc[-2]
    l4h    = df_4h.iloc[-1]
    p4h    = df_4h.iloc[-2]
    entry  = last["close"]
    atr    = last["ATR_14"]
    rsi    = last["RSI_14"]
    rsi_7  = last["RSI_7"]
    adx    = last["ADX_14"]

    if pd.isna(atr) or atr == 0 or atr < entry * 0.0001:
        return None

    score     = 0
    direction = None
    reasons   = []

    # ── Step 1: Determine direction ───────────────────────────────────────────
    ema9_1h  = last["EMA_9"]
    ema20_1h = last["EMA_20"]
    ema50_1h = last["EMA_50"]

    ema50_4h = l4h["EMA_50"]
    ema20_4h = l4h["EMA_20"]
    price_4h = l4h["close"]

    # 1H EMA direction
    if ema9_1h > ema20_1h:
        direction = "LONG"
    elif ema9_1h < ema20_1h:
        direction = "SHORT"
    else:
        return None

    # ── Step 2: Direction-specific gate ───────────────────────────────────────
    if direction == "LONG":
        # LONG gate: BTC must be bullish OR we're in a bounce from oversold
        btc_bearish = ctx.btc_is_bearish()
        rsi_was_oversold = rsi_7 < 45 and rsi > 30  # recovering from oversold

        if coin != "BTC" and btc_bearish and not rsi_was_oversold:
            return None  # block LONGs in bear market unless oversold bounce

        if coin != "BTC" and btc_bearish and rsi_was_oversold:
            score -= 10  # penalty for going against BTC but allow bounce
            reasons.append("BTC-Bear-Bounce")

    elif direction == "SHORT":
        # SHORT gate v6.0: price below 4H EMA50 OR 4H EMA50 declining
        # This works in slow bleeds where EMA cross hasn't happened yet
        price_below_ema50 = price_4h < ema50_4h
        ema50_declining   = ema50_4h < p4h["EMA_50"] * 1.001  # 4H EMA50 trending down
        ema20_below_ema50 = ema20_4h < ema50_4h                # traditional cross

        if price_below_ema50 or ema50_declining or ema20_below_ema50:
            score += 5; reasons.append("4H-Bear-Bias")
        else:
            # 4H is genuinely bullish — no point shorting
            return None

    # ── Step 3: ADX filter (relaxed for both) ─────────────────────────────────
    adx_min = 10 if direction == "SHORT" else 15
    if adx < adx_min:
        # Even with low ADX, allow if MACD just crossed
        macd_hist = last["MACD_HIST"]
        prev_hist = prev["MACD_HIST"]
        fresh_cross = (
            (direction == "LONG"  and pd.notna(macd_hist) and macd_hist > 0 and pd.notna(prev_hist) and prev_hist <= 0) or
            (direction == "SHORT" and pd.notna(macd_hist) and macd_hist < 0 and pd.notna(prev_hist) and prev_hist >= 0)
        )
        if not fresh_cross:
            return None  # no trend AND no fresh momentum = skip
        else:
            reasons.append("FreshCross-LowADX")

    # ── Pillar 1: EMA 9/20 Direction (20pts) ──────────────────────────────────
    score += 20

    # ── Pillar 2: EMA 20/50 confirmation (10pts) ──────────────────────────────
    if direction == "LONG" and ema20_1h > ema50_1h:
        score += 10; reasons.append("EMA20↑50")
    elif direction == "SHORT" and ema20_1h < ema50_1h:
        score += 10; reasons.append("EMA20↓50")
    elif direction == "LONG" and ema20_1h < ema50_1h:
        score -= 5;  reasons.append("EMA20↓50-Warn")
    else:
        score -= 5;  reasons.append("EMA20↑50-Warn")

    # ── Pillar 3: EMA 200 (8pts) ──────────────────────────────────────────────
    ema200 = last["EMA_200"]
    if pd.notna(ema200):
        if direction == "LONG" and entry > ema200:
            score += 8; reasons.append("AboveEMA200")
        elif direction == "SHORT" and entry < ema200:
            score += 8; reasons.append("BelowEMA200")
        elif direction == "LONG" and entry < ema200:
            score -= 3; reasons.append("BelowEMA200!")
        elif direction == "SHORT" and entry > ema200:
            score -= 3; reasons.append("AboveEMA200!")

    # ── Pillar 4: MACD (15pts) ────────────────────────────────────────────────
    macd      = last["MACD"]
    macd_sig  = last["MACD_SIG"]
    macd_hist = last["MACD_HIST"]
    prev_hist = df_1h["MACD_HIST"].iloc[-2] if len(df_1h) > 2 else 0

    if pd.notna(macd) and pd.notna(macd_sig) and pd.notna(macd_hist):
        if direction == "LONG":
            if macd_hist > 0 and prev_hist <= 0:
                score += 15; reasons.append("MACD-FreshCross↑")  # best signal
            elif macd_hist > 0 and macd_hist > prev_hist:
                score += 10; reasons.append("MACD↑Growing")
            elif macd_hist > 0:
                score += 6;  reasons.append("MACD↑")
            else:
                score -= 5;  reasons.append("MACD↓Warn")
        else:
            if macd_hist < 0 and prev_hist >= 0:
                score += 15; reasons.append("MACD-FreshCross↓")  # best signal
            elif macd_hist < 0 and macd_hist < prev_hist:
                score += 10; reasons.append("MACD↓Growing")
            elif macd_hist < 0:
                score += 6;  reasons.append("MACD↓")
            else:
                score -= 5;  reasons.append("MACD↑Warn")

    # ── Pillar 5: RSI (18pts) ─────────────────────────────────────────────────
    if direction == "LONG":
        if rsi < 30:
            score += 15; reasons.append(f"RSI-Oversold({rsi:.0f})")  # bouncing from oversold
        elif 30 <= rsi <= 50:
            score += 18; reasons.append(f"RSI-Sweet({rsi:.0f})")      # best entry zone
        elif rsi <= 60:
            score += 10; reasons.append(f"RSI-Mid({rsi:.0f})")
        elif rsi <= 68:
            score += 4;  reasons.append(f"RSI-High({rsi:.0f})")
        else:
            return None  # overbought — hard block
    else:
        if rsi > 70:
            score += 15; reasons.append(f"RSI-Overbought({rsi:.0f})")
        elif 50 <= rsi <= 70:
            score += 18; reasons.append(f"RSI-Sweet({rsi:.0f})")
        elif rsi >= 40:
            score += 10; reasons.append(f"RSI-Mid({rsi:.0f})")
        elif rsi >= 25:
            score += 4;  reasons.append(f"RSI-Low({rsi:.0f})")
        else:
            return None  # extremely oversold — hard block for shorts

    # ── Pillar 6: ADX (10pts) ─────────────────────────────────────────────────
    if adx >= 30:   score += 10; reasons.append(f"ADX-Strong({adx:.0f})")
    elif adx >= 22: score += 7;  reasons.append(f"ADX-Good({adx:.0f})")
    elif adx >= 15: score += 4;  reasons.append(f"ADX-Weak({adx:.0f})")
    else:           score += 1;  reasons.append(f"ADX-VeryWeak({adx:.0f})")

    # ── Pillar 7: Bollinger Band (8pts) ───────────────────────────────────────
    bb_pct = last["BB_PCT"]
    if pd.notna(bb_pct):
        if direction == "LONG":
            if bb_pct < 0.15:   score += 8; reasons.append("BB-LowerEdge")
            elif bb_pct < 0.35: score += 5; reasons.append("BB-Lower")
            elif bb_pct < 0.65: score += 2
            elif bb_pct > 0.85: score -= 5; reasons.append("BB-UpperWarn")
        else:
            if bb_pct > 0.85:   score += 8; reasons.append("BB-UpperEdge")
            elif bb_pct > 0.65: score += 5; reasons.append("BB-Upper")
            elif bb_pct > 0.35: score += 2
            elif bb_pct < 0.15: score -= 5; reasons.append("BB-LowerWarn")

    # ── Pillar 8: OBV (7pts) ──────────────────────────────────────────────────
    obv     = last["OBV"]
    obv_ema = last["OBV_EMA"]
    if pd.notna(obv_ema):
        if direction == "LONG" and obv > obv_ema:
            score += 7; reasons.append("OBV↑")
        elif direction == "SHORT" and obv < obv_ema:
            score += 7; reasons.append("OBV↓")
        elif direction == "LONG" and obv < obv_ema:
            score -= 5; reasons.append("OBV↓Warn")
        elif direction == "SHORT" and obv > obv_ema:
            score -= 5; reasons.append("OBV↑Warn")

    # ── Pillar 9: Volume (5pts) ───────────────────────────────────────────────
    vol_ma = last["VOL_MA20"]
    if pd.notna(vol_ma) and vol_ma > 0:
        vr = last["vol"] / vol_ma
        if vr >= 1.5:   score += 5; reasons.append(f"Vol({vr:.1f}x)")
        elif vr >= 1.0: score += 2
        else:           score -= 2

    # ── Pillar 10: Funding Rate (8pts) ────────────────────────────────────────
    if funding_rate is not None:
        fr = funding_rate
        if direction == "LONG":
            if fr < -0.0001:        score += 8; reasons.append(f"FR+({fr:.3%})")
            elif abs(fr) <= 0.0005: score += 3
        else:
            if fr > 0.0001:         score += 8; reasons.append(f"FR-({fr:.3%})")
            elif abs(fr) <= 0.0005: score += 3

    # ── Pillar 11: Fear & Greed (5pts) ────────────────────────────────────────
    fg = ctx.fear_greed
    if direction == "LONG":
        if fg < 25:   score += 5; reasons.append(f"ExtremeFear({fg})")
        elif fg < 40: score += 3; reasons.append(f"Fear({fg})")
        elif fg > 75: score -= 5; reasons.append(f"Greed!({fg})")
    else:
        if fg > 75:   score += 5; reasons.append(f"ExtremeGreed({fg})")
        elif fg >= 50:score += 2
        elif fg < 25: score += 3; reasons.append(f"FearSHORT({fg})")  # fear confirms shorts

    # ── Pillar 12: L/S Ratio (4pts) ───────────────────────────────────────────
    ls = ctx.ls_ratio
    if direction == "LONG" and ls < 0.8:
        score += 4; reasons.append(f"L/S-Short({ls:.2f})")
    elif direction == "SHORT" and ls > 1.3:
        score += 4; reasons.append(f"L/S-Long({ls:.2f})")

    # ── Pillar 13: Candle Pattern (8pts) ──────────────────────────────────────
    cp, cr = detect_candle(df_1h, direction)
    score += cp
    if cr and cr != "Neutral": reasons.append(cr)

    # ── Macro penalty ─────────────────────────────────────────────────────────
    if ctx.macro_event_today:
        pen = 15 if ctx.macro_event_impact == "HIGH" else 8
        score -= pen; reasons.append(f"Macro-{pen}")

    # ── BTC dominance penalty ─────────────────────────────────────────────────
    if direction == "LONG" and coin != "BTC" and ctx.btc_dominance > 57:
        score -= 5; reasons.append(f"HighDom({ctx.btc_dominance:.0f}%)")

    # ── OI adjustment ─────────────────────────────────────────────────────────
    oi = ctx.oi_change_pct
    if direction == "SHORT" and oi < -1.0:
        score -= 3; reasons.append("OI↓-Caution")  # OI falling = shorts covering
    elif direction == "SHORT" and oi > 1.0:
        score += 4; reasons.append("OI↑-Confirms")  # OI rising = new shorts entering
    elif direction == "LONG" and oi > 1.0:
        score += 3; reasons.append("OI↑")

    score = max(0, score)
    if score < MANUAL_THRESHOLD - 15:
        return None  # early exit for clearly failing signals

    # ── TP/SL ─────────────────────────────────────────────────────────────────
    if direction == "LONG":
        sl   = entry - atr * 1.5; icon = "🟢"
    else:
        sl   = entry + atr * 1.5; icon = "🔴"

    if abs(sl - entry) < entry * 0.0001:
        return None

    # ── S/R check (simplified — don't penalise as harshly) ───────────────────
    res_levels, sup_levels = find_sr_levels(df_1h)
    tp_main = entry + atr * 3.0 if direction == "LONG" else entry - atr * 3.0

    if direction == "LONG":
        blockers = [r for r in res_levels if entry * 1.005 < r < tp_main]
        if blockers and (min(blockers) - entry) / (tp_main - entry + 1e-9) < 0.3:
            score -= 8; reasons.append(f"SRBlock@{min(blockers):.4g}")
        elif not blockers:
            score += 5; reasons.append("PathClear")
    else:
        blockers = [s for s in sup_levels if tp_main < s < entry * 0.995]
        if blockers and (entry - max(blockers)) / (entry - tp_main + 1e-9) < 0.3:
            score -= 8; reasons.append(f"SRBlock@{max(blockers):.4g}")
        elif not blockers:
            score += 5; reasons.append("PathClear")

    score = max(0, score)
    if score < MANUAL_THRESHOLD:
        return None

    # ── Build result ──────────────────────────────────────────────────────────
    tp1, tp2, tp3 = calc_tps(entry, atr, direction, res_levels, sup_levels)
    sl_pct        = abs(entry - sl) / entry
    lev           = min(20, max(1, round(0.02 / sl_pct))) if sl_pct > 0 else 1
    rr            = round(abs(tp2 - entry) / abs(sl - entry), 2)
    liq_est       = entry * 0.92 if direction == "LONG" else entry * 1.08

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
        "news"         : ctx.news_sentiment.get(coin, "NEUTRAL"),
        "news_headline": ctx.news_headlines.get(coin, ""),
        "liq_est"      : round(liq_est, 6),
        "sl_pct"       : round(sl_pct * 100, 2),
        "reasons"      : " | ".join(reasons),
    }


# ─── Correlation Filter ────────────────────────────────────────────────────────

def filter_correlated(signals):
    final, longs, shorts = [], 0, 0
    for sig in signals:
        is_long = "LONG" in sig["dir"]
        if is_long  and longs  >= MAX_LONGS:  continue
        if not is_long and shorts >= MAX_SHORTS: continue
        longs  += 1 if is_long else 0
        shorts += 0 if is_long else 1
        final.append(sig)
        if len(final) >= MAX_SIGNALS: break
    return final


# ─── Main Entry ───────────────────────────────────────────────────────────────

async def get_top_signals():
    """
    Full pipeline with ban detection.
    Returns (signals, context).
    """
    # Ban check FIRST — before any Binance call
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

        logger.info("Fetching tickers (top 100)...")
        try:
            tickers = await exchange.fetch_tickers(all_futures[:100])
        except Exception as e:
            if "418" in str(e):
                match = re.search(r"banned until (\d+)", str(e))
                if match: save_ban(int(match.group(1)))
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
            try:
                r = await analyze_symbol(
                    exchange, sym,
                    tickers.get(sym, {}),
                    funding_map.get(sym),
                    ctx
                )
                if r:
                    raw.append(r)
                    logger.info(f"PASS {r['symbol']:15s} {r['dir']} Score:{r['score']} | {r['reasons'][:60]}")
            except Exception as e:
                logger.debug(f"Symbol analysis failed {sym}: {e}")
            await asyncio.sleep(0.8)

        raw.sort(key=lambda x: x["score"], reverse=True)
        final = filter_correlated(raw)
        logger.info(f"Scan done. Passed:{len(raw)} Final:{len(final)}")
        return final, ctx

    except Exception as e:
        err = str(e)
        if "418" in err or "banned until" in err.lower():
            match = re.search(r"banned until (\d+)", err)
            if match:
                save_ban(int(match.group(1)))
            else:
                save_ban(int((time.time() + 7200) * 1000))
        else:
            logger.error(f"get_top_signals error: {e}", exc_info=True)
        return [], MarketContext()

    finally:
        await exchange.close()
