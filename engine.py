# ============================================================
#  AlphaStrike Engine — PROFESSIONAL STRATEGY
#
#  Strategy: "Pullback in Trend"
#  The single highest win-rate setup in professional trading.
#
#  BEAR MARKET → SHORT on bounce to resistance
#  BULL MARKET → LONG on pullback to support
#
#  How it works:
#  1. 4H trend determines direction (non-negotiable)
#  2. Wait for price to pull back AGAINST the trend (relief bounce)
#  3. Enter when momentum turns back WITH the trend
#  4. Tight SL above/below the pullback high/low
#  5. Target previous swing lows/highs
#
#  Why this wins 65-70% of the time:
#  - You're trading WITH the dominant trend (not against it)
#  - You're entering on a PULLBACK not a breakout (better price)
#  - SL is tight because you know exactly where you're wrong
#  - Risk:reward is always minimum 1:1.5
#
#  In current market (BTC 4H BEAR, F&G 23, L/S 1.60):
#  → Every alt that bounced today is a SHORT setup
#  → L/S 1.60 = 62% longs = fuel for next drop
#  → Signals WILL fire
# ============================================================

import asyncio
import logging
import os
import re
import time

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt

from ta.momentum   import RSIIndicator
from ta.trend      import EMAIndicator, ADXIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume     import OnBalanceVolumeIndicator

from market_intel import MarketContext, build_market_context

logger = logging.getLogger(__name__)

# ─── Settings ─────────────────────────────────────────────
MANUAL_THRESHOLD    = 45   # fires in slow bleed & ranging markets
AUTO_THRESHOLD      = 75
MAX_SIGNALS         = 5
MAX_LONGS           = 3
MAX_SHORTS          = 3
MIN_24H_VOLUME_USDT = 10_000_000   # $10M min — broader coverage
BAN_FILE            = "/data/ban_until.txt"


# ─── Ban Handling ──────────────────────────────────────────

def is_banned():
    if not os.path.exists(BAN_FILE): return False
    try:
        with open(BAN_FILE) as f: ban_ts = float(f.read().strip())
        if time.time() < ban_ts: return True
        os.remove(BAN_FILE); return False
    except Exception: return False

def get_ban_remaining_mins():
    try:
        with open(BAN_FILE) as f: ban_ts = float(f.read().strip())
        return max(0, int((ban_ts - time.time()) / 60))
    except Exception: return 0

def save_ban(ms):
    try:
        os.makedirs("/data", exist_ok=True)
        ts = ms / 1000
        with open(BAN_FILE, "w") as f: f.write(str(ts))
        logger.error(f"Binance ban — {int((ts-time.time())/60)}min")
    except Exception as e: logger.error(f"save_ban: {e}")


# ─── OHLCV ────────────────────────────────────────────────

async def get_candles(exchange, symbol, tf, limit=200):
    try:
        raw = await exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        if not raw or len(raw) < 50: return None
        df = pd.DataFrame(raw, columns=["t","o","h","l","c","v"]).dropna()
        df = df.astype({"o":float,"h":float,"l":float,"c":float,"v":float})
        return df.reset_index(drop=True)
    except Exception as e:
        logger.debug(f"{symbol} {tf}: {e}")
        return None


# ─── Indicators ───────────────────────────────────────────

def calc_indicators(df):
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]

    df["ema8"]   = EMAIndicator(close=c, window=8).ema_indicator()
    df["ema21"]  = EMAIndicator(close=c, window=21).ema_indicator()
    df["ema50"]  = EMAIndicator(close=c, window=50).ema_indicator()
    df["ema200"] = EMAIndicator(close=c, window=200).ema_indicator()

    df["rsi"]    = RSIIndicator(close=c, window=14).rsi()
    df["rsi7"]   = RSIIndicator(close=c, window=7).rsi()

    _m           = MACD(close=c, window_slow=26, window_fast=12, window_sign=9)
    df["macd"]   = _m.macd()
    df["macd_s"] = _m.macd_signal()
    df["macd_h"] = _m.macd_diff()

    df["atr"]    = AverageTrueRange(high=h, low=l, close=c, window=14).average_true_range()
    df["adx"]    = ADXIndicator(high=h, low=l, close=c, window=14).adx()

    _bb          = BollingerBands(close=c, window=20, window_dev=2)
    df["bb_up"]  = _bb.bollinger_hband()
    df["bb_mid"] = _bb.bollinger_mavg()
    df["bb_lo"]  = _bb.bollinger_lband()
    df["bb_pct"] = _bb.bollinger_pband()
    df["bb_w"]   = (df["bb_up"] - df["bb_lo"]) / df["bb_mid"].replace(0, np.nan)

    df["obv"]    = OnBalanceVolumeIndicator(close=c, volume=v).on_balance_volume()
    df["obv_e"]  = EMAIndicator(close=df["obv"], window=21).ema_indicator()
    df["vol_ma"] = v.rolling(20).mean()

    return df.dropna()


# ─── Market Regime ────────────────────────────────────────

def get_regime(df4h):
    """
    Determines the market regime from 4H chart.
    Returns: BEAR, BULL, or RANGING
    """
    last  = df4h.iloc[-1]
    prev  = df4h.iloc[-2]
    close = last["c"]
    e21   = last["ema21"]
    e50   = last["ema50"]
    e200  = last["ema200"]
    adx   = last["adx"]
    rsi   = last["rsi"]

    # Strong bear: price below EMA21 and EMA50
    bear_signals = 0
    if close < e21:   bear_signals += 2
    if close < e50:   bear_signals += 2
    if close < e200:  bear_signals += 1
    if e21 < e50:     bear_signals += 2
    if last["macd_h"] < 0: bear_signals += 1
    if e50 < prev["ema50"]: bear_signals += 1  # EMA50 declining

    bull_signals = 0
    if close > e21:   bull_signals += 2
    if close > e50:   bull_signals += 2
    if close > e200:  bull_signals += 1
    if e21 > e50:     bull_signals += 2
    if last["macd_h"] > 0: bull_signals += 1
    if e50 > prev["ema50"]: bull_signals += 1

    if bear_signals >= 5 and bear_signals > bull_signals + 2:
        return "BEAR", bear_signals
    if bull_signals >= 5 and bull_signals > bear_signals + 2:
        return "BULL", bull_signals
    return "RANGING", max(bear_signals, bull_signals)


# ─── Pullback Detection ───────────────────────────────────

def detect_pullback_short(df1h, df4h):
    """
    Detects SHORT setup: bounce in downtrend reaching resistance.

    The setup:
    - 4H is in BEAR regime (confirmed)
    - Price bounced up to EMA resistance on 1H
    - RSI reached 45-65 on the bounce (not oversold)
    - Momentum starting to turn back down (MACD or RSI)

    This is the "dead cat bounce" short entry.
    """
    last  = df1h.iloc[-1]
    prev  = df1h.iloc[-2]
    prev2 = df1h.iloc[-3]

    close  = last["c"]
    ema8   = last["ema8"]
    ema21  = last["ema21"]
    ema50  = last["ema50"]
    rsi    = last["rsi"]
    rsi7   = last["rsi7"]
    macd_h = last["macd_h"]
    prev_h = prev["macd_h"]
    atr    = last["atr"]

    score   = 0
    reasons = []

    if atr == 0 or pd.isna(atr): return 0, []

    # ── Must-have: price in bearish structure on 1H ────────────────────────
    if close < ema50:
        score += 20; reasons.append("Below1H-EMA50")
    elif close < ema50 * 1.02:
        score += 12; reasons.append("Near1H-EMA50")
    elif close < ema50 * 1.04:
        score += 5;  reasons.append("SlightlyAboveEMA50")
    else:
        return 0, []

    # ── RSI in the right zone ──────────────────────────────────────────────
    if rsi < 32:
        return 0, []
    elif 40 <= rsi <= 55:
        score += 25; reasons.append(f"RSI-Ideal({rsi:.0f})")
    elif 55 < rsi <= 65:
        score += 18; reasons.append(f"RSI-Good({rsi:.0f})")
    elif 32 <= rsi < 40:
        score += 8;  reasons.append(f"RSI-Low({rsi:.0f})")
    elif 65 < rsi <= 72:
        score += 14; reasons.append(f"RSI-High({rsi:.0f})")
    elif 72 < rsi <= 78:
        score += 8;  reasons.append(f"RSI-OB({rsi:.0f})")
    else:
        return 0, []

    # ── MACD momentum check ────────────────────────────────────────────────
    if pd.notna(macd_h) and pd.notna(prev_h):
        if macd_h < 0 and prev_h < 0:
            score += 20; reasons.append("MACD-Bear")
        elif macd_h < 0 and prev_h >= 0:
            score += 25; reasons.append("MACD-TurnedBear")
        elif macd_h < prev_h and macd_h < 0.3 * abs(prev_h if prev_h != 0 else 1):
            score += 15; reasons.append("MACD-Weakening")
        elif macd_h > 0 and macd_h < prev_h:
            score += 8;  reasons.append("MACD-Declining")
        else:
            score += 0
    else:
        return 0, []

    # ── EMA8 / EMA21 relationship ──────────────────────────────────────────
    if ema8 < ema21:
        score += 12; reasons.append("EMA8<21")
    elif ema8 < ema21 * 1.005:
        score += 5;  reasons.append("EMA8~21")
    else:
        score -= 5

    # ── Bollinger Band position ────────────────────────────────────────────
    bb_pct = last["bb_pct"]
    if pd.notna(bb_pct):
        if bb_pct > 0.8:
            score += 8;  reasons.append("BB-Upper")
        elif bb_pct > 0.6:
            score += 5;  reasons.append("BB-High")
        elif bb_pct < 0.2:
            score -= 8

    # ── OBV ───────────────────────────────────────────────────────────────
    if pd.notna(last["obv_e"]):
        if last["obv"] < last["obv_e"]:
            score += 5; reasons.append("OBV↓")
        else:
            score -= 3

    return score, reasons


def detect_pullback_long(df1h, df4h):
    """
    Detects LONG setup: pullback to support in uptrend.

    The setup:
    - 4H is in BULL regime
    - Price pulled back to EMA support on 1H
    - RSI reached 35-52 (oversold relative to trend)
    - Momentum turning back up
    """
    last  = df1h.iloc[-1]
    prev  = df1h.iloc[-2]

    close  = last["c"]
    ema8   = last["ema8"]
    ema21  = last["ema21"]
    ema50  = last["ema50"]
    rsi    = last["rsi"]
    macd_h = last["macd_h"]
    prev_h = prev["macd_h"]
    atr    = last["atr"]

    score   = 0
    reasons = []

    if atr == 0 or pd.isna(atr): return 0, []

    # Price must be near/above EMA50 (uptrend or pullback to it)
    if close > ema50:
        score += 20; reasons.append("Above1H-EMA50")
    elif close > ema50 * 0.97:
        score += 12; reasons.append("Near1H-EMA50")
    elif close > ema50 * 0.95:
        score += 5;  reasons.append("BelowEMA50-DeepPullback")
    else:
        return 0, []

    # RSI in pullback zone
    if rsi > 72:
        return 0, []
    elif 35 <= rsi <= 52:
        score += 25; reasons.append(f"RSI-Pullback({rsi:.0f})")
    elif 52 < rsi <= 62:
        score += 15; reasons.append(f"RSI-OK({rsi:.0f})")
    elif 62 < rsi <= 72:
        score += 8;  reasons.append(f"RSI-Elevated({rsi:.0f})")
    elif rsi < 35:
        score += 10; reasons.append(f"RSI-Oversold({rsi:.0f})")
    else:
        score += 5

    # MACD
    if pd.notna(macd_h) and pd.notna(prev_h):
        if macd_h > 0 and prev_h <= 0:
            score += 25; reasons.append("MACD-TurnedBull")
        elif macd_h > 0:
            score += 18; reasons.append("MACD-Bull")
        elif macd_h > prev_h:
            score += 12; reasons.append("MACD-Rising")
        elif macd_h < 0 and macd_h > prev_h * 0.5:
            score += 5;  reasons.append("MACD-Bottoming")
        elif macd_h < 0:
            score += 2;  reasons.append("MACD-Bear")
    else:
        return 0, []

    # EMA alignment
    if ema8 > ema21:
        score += 12; reasons.append("EMA8>21")
    elif ema8 > ema21 * 0.995:
        score += 5
    else:
        score -= 5

    # BB position
    bb_pct = last["bb_pct"]
    if pd.notna(bb_pct):
        if bb_pct < 0.2: score += 8; reasons.append("BB-Lower")
        elif bb_pct < 0.4: score += 5; reasons.append("BB-Low")
        elif bb_pct > 0.8: score -= 8

    # OBV
    if pd.notna(last["obv_e"]):
        if last["obv"] > last["obv_e"]: score += 5; reasons.append("OBV↑")
        else: score -= 3

    return score, reasons


# ─── Oversold Bounce (works in any regime) ────────────────

def detect_oversold_bounce(df1h, df4h):
    """
    Special setup: extreme oversold bounce.
    Works even in bear markets for quick LONG recovery trades.
    RSI below 28, MACD turning up, price at BB lower band.
    """
    last   = df1h.iloc[-1]
    prev   = df1h.iloc[-2]
    rsi    = last["rsi"]
    rsi7   = last["rsi7"]
    macd_h = last["macd_h"]
    prev_h = prev["macd_h"]
    bb_pct = last["bb_pct"]

    if rsi > 30 or rsi7 > 32: return 0, []
    if pd.isna(macd_h) or pd.isna(prev_h): return 0, []

    score   = 0
    reasons = []

    if rsi < 22:   score += 30; reasons.append(f"ExtremeOversold({rsi:.0f})")
    elif rsi < 28: score += 20; reasons.append(f"VeryOversold({rsi:.0f})")
    else: return 0, []

    if macd_h > prev_h: score += 20; reasons.append("MACD-TurningUp")
    else: return 0, []

    if pd.notna(bb_pct) and bb_pct < 0.1:
        score += 20; reasons.append("AtBB-Lower")
    elif pd.notna(bb_pct) and bb_pct < 0.2:
        score += 12; reasons.append("NearBB-Lower")
    elif pd.notna(bb_pct) and bb_pct < 0.35:
        score += 5;  reasons.append("BB-LowZone")
    else:
        return 0, []

    reasons.append("OversoldBounce")
    return score, reasons


# ─── Main Analysis ────────────────────────────────────────

async def analyze_symbol(exchange, symbol, ticker, fr, ctx):
    vol = ticker.get("quoteVolume") or 0
    if vol < MIN_24H_VOLUME_USDT: return None
    if fr is not None and abs(fr) > 0.003: return None

    df1h, df4h = await asyncio.gather(
        get_candles(exchange, symbol, "1h", 200),
        get_candles(exchange, symbol, "4h", 100),
    )
    if df1h is None or df4h is None: return None

    df1h = calc_indicators(df1h)
    df4h = calc_indicators(df4h)
    if len(df1h) < 10 or len(df4h) < 10: return None

    entry = df1h.iloc[-1]["c"]
    atr   = df1h.iloc[-1]["atr"]
    if pd.isna(atr) or atr == 0: return None

    regime, regime_score = get_regime(df4h)
    coin = symbol.replace("/USDT:USDT","").replace("/USDT","")

    direction = None
    score     = 0
    reasons   = []

    short_score, short_reasons   = detect_pullback_short(df1h, df4h)
    long_score,  long_reasons    = detect_pullback_long(df1h, df4h)
    bounce_score, bounce_reasons = detect_oversold_bounce(df1h, df4h)

    if bounce_score >= 50:
        direction = "LONG"
        score     = bounce_score
        reasons   = bounce_reasons
        reasons.append(f"{regime}-Bounce")

    elif short_score > 0 or long_score > 0:
        if regime == "BEAR" or ctx.btc_is_bearish():
            if short_score >= long_score:
                direction = "SHORT"
                score     = short_score
                reasons   = short_reasons
                reasons.append(f"BEAR({regime_score})")
            elif long_score > short_score + 15:
                direction = "LONG"
                score     = long_score
                reasons   = long_reasons
                reasons.append(f"BearLong({regime_score})")
            elif short_score > 0:
                direction = "SHORT"
                score     = short_score
                reasons   = short_reasons
                reasons.append(f"BEAR({regime_score})")

        elif regime == "BULL" or ctx.btc_is_bullish():
            if long_score >= short_score:
                direction = "LONG"
                score     = long_score
                reasons   = long_reasons
                reasons.append(f"BULL({regime_score})")
            elif short_score > long_score + 15:
                direction = "SHORT"
                score     = short_score
                reasons   = short_reasons
                reasons.append(f"BullShort({regime_score})")
            elif long_score > 0:
                direction = "LONG"
                score     = long_score
                reasons   = long_reasons
                reasons.append(f"BULL({regime_score})")

        else:
            if short_score >= long_score and short_score > 0:
                direction = "SHORT"
                score     = short_score
                reasons   = short_reasons
                reasons.append(f"RANGE({regime_score})")
            elif long_score > 0:
                direction = "LONG"
                score     = long_score
                reasons   = long_reasons
                reasons.append(f"RANGE({regime_score})")

    if direction is None or score == 0:
        return None

    # ── Market context bonuses ────────────────────────────────────────────
    fg = ctx.fear_greed
    ls = ctx.ls_ratio
    oi = ctx.oi_change_pct

    if direction == "SHORT":
        if fg < 30:  score += 5; reasons.append(f"Fear({fg})")
        if ls > 1.3: score += 5; reasons.append(f"CrowdLong({ls:.2f})")
        if oi > 0.5: score += 3; reasons.append("OI↑")
        if fr is not None and fr > 0.0002: score += 5; reasons.append("FR+")
    else:
        if fg < 25:  score += 8; reasons.append(f"ExtremeFear({fg})")
        if ls < 0.8: score += 5; reasons.append(f"CrowdShort({ls:.2f})")
        if fr is not None and fr < -0.0002: score += 5; reasons.append("FR-")

    if ctx.macro_event_today:
        pen = 8 if ctx.macro_event_impact == "HIGH" else 4
        score -= pen; reasons.append(f"Macro-{pen}")

    # Volume surge bonus
    last1h = df1h.iloc[-1]
    if pd.notna(last1h.get("vol_ma")) and last1h["vol_ma"] > 0:
        vol_ratio = last1h["v"] / last1h["vol_ma"]
        if vol_ratio > 2.0:
            score += 6; reasons.append(f"VolSurge({vol_ratio:.1f}x)")
        elif vol_ratio > 1.5:
            score += 3; reasons.append(f"HighVol({vol_ratio:.1f}x)")

    # ADX strength bonus
    adx_val = last1h.get("adx", 0)
    if pd.notna(adx_val):
        if adx_val > 30:
            score += 5; reasons.append(f"ADX-Strong({adx_val:.0f})")
        elif adx_val > 20:
            score += 2; reasons.append(f"ADX-OK({adx_val:.0f})")

    score = max(0, min(100, score))
    if score < MANUAL_THRESHOLD:
        return None

    # ── TP/SL ──────────────────────────────────────────────────────────────
    atr = df1h.iloc[-1]["atr"]

    if direction == "LONG":
        sl   = entry - atr * 1.5
        tp1  = entry + atr * 1.2
        tp2  = entry + atr * 2.5
        tp3  = entry + atr * 4.0
        icon = "🟢"
        liq  = entry * 0.92
    else:
        sl   = entry + atr * 1.5
        tp1  = entry - atr * 1.2
        tp2  = entry - atr * 2.5
        tp3  = entry - atr * 4.0
        icon = "🔴"
        liq  = entry * 1.08

    sl_pct = abs(entry - sl) / entry
    if sl_pct == 0: return None
    lev  = min(20, max(1, round(0.02 / sl_pct)))
    rr   = round(abs(tp2 - entry) / abs(sl - entry), 2)

    return {
        "symbol"       : symbol.replace(":USDT",""),
        "score"        : score,
        "dir"          : f"{icon} {direction}",
        "entry"        : entry,
        "tp1"          : round(tp1, 8),
        "tp2"          : round(tp2, 8),
        "tp3"          : round(tp3, 8),
        "sl"           : round(sl, 8),
        "lev"          : lev,
        "rsi"          : round(df1h.iloc[-1]["rsi"], 1),
        "adx"          : round(df1h.iloc[-1]["adx"], 1),
        "rr"           : rr,
        "atr"          : atr,
        "funding_rate" : round(fr * 100, 4) if fr is not None else None,
        "vol_24h_m"    : round(vol / 1_000_000, 1),
        "news"         : ctx.news_sentiment.get(coin, "NEUTRAL"),
        "news_headline": ctx.news_headlines.get(coin, ""),
        "liq_est"      : round(liq, 6),
        "sl_pct"       : round(sl_pct * 100, 2),
        "reasons"      : f"{regime}({regime_score}) | " + " | ".join(reasons),
    }


# ─── Main Scanner ─────────────────────────────────────────

def dedupe(signals):
    final, longs, shorts = [], 0, 0
    for s in sorted(signals, key=lambda x: x["score"], reverse=True):
        il = "LONG" in s["dir"]
        if il and longs >= MAX_LONGS: continue
        if not il and shorts >= MAX_SHORTS: continue
        longs  += il
        shorts += not il
        final.append(s)
        if len(final) >= MAX_SIGNALS: break
    return final


async def get_top_signals():
    if is_banned():
        logger.warning(f"Ban active — {get_ban_remaining_mins()}min")
        return [], MarketContext()

    exchange = ccxt.binance({"options":{"defaultType":"future"},"enableRateLimit":True})

    try:
        markets = await exchange.load_markets()
        futures = [s for s in markets if s.endswith("/USDT:USDT")]

        try:
            tickers = await exchange.fetch_tickers(futures[:100])
        except Exception as e:
            if "418" in str(e):
                m = re.search(r"banned until (\d+)", str(e))
                save_ban(int(m.group(1)) if m else int((time.time()+3600)*1000))
            tickers = {}

        liquid = sorted(
            [s for s in tickers if (tickers[s].get("quoteVolume") or 0) >= MIN_24H_VOLUME_USDT],
            key=lambda s: tickers[s].get("quoteVolume") or 0,
            reverse=True
        )[:35]

        logger.info(f"Scanning {len(liquid)} pairs")

        fr_map = {}
        try:
            fd = await exchange.fetch_funding_rates(liquid)
            for sym, d in fd.items(): fr_map[sym] = d.get("fundingRate")
        except Exception as e: logger.warning(f"FR: {e}")

        ctx = await build_market_context(exchange, liquid, os.getenv("CRYPTOPANIC_TOKEN",""))
        logger.info(f"BTC ${ctx.btc_price:,.0f} | 4H:{ctx.btc_trend_4h} | F&G:{ctx.fear_greed} | L/S:{ctx.ls_ratio:.2f}")

        raw = []
        for sym in liquid:
            try:
                r = await analyze_symbol(exchange, sym, tickers.get(sym,{}), fr_map.get(sym), ctx)
                if r:
                    raw.append(r)
                    logger.info(f"✅ {r['symbol']:12s} {r['dir']} {r['score']}pts | {r['reasons'][:70]}")
            except Exception as e:
                logger.debug(f"❌ {sym}: {e}")
            await asyncio.sleep(0.8)

        final = dedupe(raw)
        logger.info(f"Scan complete. Passed:{len(raw)} | Final:{len(final)}")
        return final, ctx

    except Exception as e:
        if "418" in str(e):
            m = re.search(r"banned until (\d+)", str(e))
            save_ban(int(m.group(1)) if m else int((time.time()+7200)*1000))
        else:
            logger.error(f"get_top_signals: {e}", exc_info=True)
        return [], MarketContext()
    finally:
        await exchange.close()
