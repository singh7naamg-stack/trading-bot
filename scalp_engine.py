# ============================================================
#  QuestLife Signal Bot — scalp_engine.py
#  5m + 15m scalp scanner — works in ranging/sideways markets
#  Targets: 0.5-1.5% | SL: 0.3-0.5% | Hold: 10-45 min
# ============================================================

import asyncio
import logging
import os

import pandas as pd
import ccxt.async_support as ccxt

from ta.momentum   import RSIIndicator
from ta.trend      import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume     import OnBalanceVolumeIndicator

logger = logging.getLogger(__name__)

# ─── Scalp Settings ───────────────────────────────────────────────────────────
SCALP_MIN_VOLUME    = 50_000_000   # $50M min 24H volume (higher than swing)
SCALP_MIN_ADX       = 10           # much lower than swing — works in ranging
SCALP_THRESHOLD     = 60           # 60% min score to fire scalp signal
SCALP_MAX_SIGNALS   = 3            # max 3 scalp signals per scan
SCALP_MAX_LONGS     = 2
SCALP_MAX_SHORTS    = 2

# Best pairs for scalping — highest liquidity = tightest spreads
SCALP_PAIRS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
    "AVAX/USDT:USDT",
    "LTC/USDT:USDT",
    "LINK/USDT:USDT",
    "ADA/USDT:USDT",
    "DOT/USDT:USDT",
    "MATIC/USDT:USDT",
    "OP/USDT:USDT",
    "ARB/USDT:USDT",
    "NEAR/USDT:USDT",
]


# ─── OHLCV Fetch ──────────────────────────────────────────────────────────────

async def fetch_scalp_candles(exchange, symbol, timeframe, limit):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 30:
            return None
        df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","vol"])
        return df.dropna()
    except Exception as e:
        logger.debug(f"Scalp OHLCV [{symbol} {timeframe}]: {e}")
        return None


# ─── Scalp Indicators ─────────────────────────────────────────────────────────

def add_scalp_indicators(df):
    df    = df.copy()
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["vol"]

    # EMAs for trend direction
    df["EMA_9"]    = EMAIndicator(close=close, window=9).ema_indicator()
    df["EMA_21"]   = EMAIndicator(close=close, window=21).ema_indicator()
    df["EMA_50"]   = EMAIndicator(close=close, window=50).ema_indicator()

    # RSI
    df["RSI_7"]    = RSIIndicator(close=close, window=7).rsi()   # fast RSI for scalping
    df["RSI_14"]   = RSIIndicator(close=close, window=14).rsi()

    # MACD fast settings for 5m
    _macd          = MACD(close=close, window_slow=12, window_fast=6, window_sign=4)
    df["MACD"]     = _macd.macd()
    df["MACD_SIG"] = _macd.macd_signal()
    df["MACD_HIST"]= _macd.macd_diff()

    # ATR for SL/TP
    df["ATR"]      = AverageTrueRange(high=high, low=low, close=close, window=7).average_true_range()

    # Bollinger Bands
    _bb            = BollingerBands(close=close, window=20, window_dev=2)
    df["BB_UP"]    = _bb.bollinger_hband()
    df["BB_MID"]   = _bb.bollinger_mavg()
    df["BB_LOW"]   = _bb.bollinger_lband()
    df["BB_WIDTH"] = (df["BB_UP"] - df["BB_LOW"]) / df["BB_MID"].replace(0, float("nan"))
    df["BB_PCT"]   = _bb.bollinger_pband()  # 0=at lower band, 1=at upper band

    # OBV
    df["OBV"]      = OnBalanceVolumeIndicator(close=close, volume=vol).on_balance_volume()
    df["OBV_EMA"]  = EMAIndicator(close=df["OBV"], window=9).ema_indicator()

    # Volume
    df["VOL_MA10"] = vol.rolling(window=10).mean()
    df["VOL_MA20"] = vol.rolling(window=20).mean()

    return df


# ─── Scalp Signal Logic ───────────────────────────────────────────────────────

async def analyze_scalp(exchange, symbol, ticker):
    """
    Scalp analysis on 5m + 15m timeframes.
    Simplified scoring — works in ranging and trending markets.
    Looks for:
      - Momentum building (MACD crossover on 5m)
      - RSI in sweet spot (not overbought/oversold)
      - Price near BB edge (bounce opportunity)
      - Volume spike confirming move
      - 15m trend not opposing
    """
    coin      = symbol.replace("/USDT:USDT","").replace("/USDT","")
    quote_vol = ticker.get("quoteVolume") or 0

    if quote_vol < SCALP_MIN_VOLUME:
        return None

    # Fetch 5m and 15m candles
    df_5m, df_15m = await asyncio.gather(
        fetch_scalp_candles(exchange, symbol, "5m",  100),
        fetch_scalp_candles(exchange, symbol, "15m", 60),
    )
    if df_5m is None or df_15m is None:
        return None

    df_5m  = add_scalp_indicators(df_5m).dropna()
    df_15m = add_scalp_indicators(df_15m).dropna()

    if len(df_5m) < 5 or len(df_15m) < 3:
        return None

    last_5m  = df_5m.iloc[-1]
    prev_5m  = df_5m.iloc[-2]
    last_15m = df_15m.iloc[-1]

    entry  = last_5m["close"]
    atr    = last_5m["ATR"]
    rsi_7  = last_5m["RSI_7"]
    rsi_14 = last_5m["RSI_14"]

    if pd.isna(atr) or atr == 0:
        return None

    # SL distance must be reasonable (0.2% - 0.8%)
    sl_atr   = atr * 1.2
    sl_pct   = sl_atr / entry
    if sl_pct < 0.002 or sl_pct > 0.012:
        return None

    score     = 0
    direction = None
    reasons   = []

    # ── Pillar 1: EMA 9/21 Direction (25pts) ──────────────────────────────────
    if last_5m["EMA_9"] > last_5m["EMA_21"]:
        score += 25; direction = "LONG"; reasons.append("EMA9↑21")
    elif last_5m["EMA_9"] < last_5m["EMA_21"]:
        score += 25; direction = "SHORT"; reasons.append("EMA9↓21")
    else:
        return None

    # ── Pillar 2: 15m trend confirmation (15pts) ──────────────────────────────
    # Don't need perfect alignment, just not strongly opposing
    if direction == "LONG":
        if last_15m["EMA_9"] > last_15m["EMA_21"]:
            score += 15; reasons.append("15m✅")
        elif last_15m["EMA_9"] > last_15m["EMA_21"] * 0.998:
            score += 8;  reasons.append("15mNeutral")
        else:
            score += 0;  reasons.append("15m⚠️")
    else:
        if last_15m["EMA_9"] < last_15m["EMA_21"]:
            score += 15; reasons.append("15m✅")
        elif last_15m["EMA_9"] < last_15m["EMA_21"] * 1.002:
            score += 8;  reasons.append("15mNeutral")
        else:
            score += 0;  reasons.append("15m⚠️")

    # ── Pillar 3: MACD Momentum (20pts) ───────────────────────────────────────
    # Fresh crossover on 5m = strong signal
    macd_hist      = last_5m["MACD_HIST"]
    prev_macd_hist = prev_5m["MACD_HIST"]

    if direction == "LONG":
        if pd.notna(macd_hist):
            if macd_hist > 0 and prev_macd_hist <= 0:
                score += 20; reasons.append("MACD-Cross↑")  # fresh bullish cross
            elif macd_hist > 0 and macd_hist > prev_macd_hist:
                score += 12; reasons.append("MACD↑Grow")     # growing bullish
            elif macd_hist > 0:
                score += 6;  reasons.append("MACD↑")
            else:
                score -= 5;  reasons.append("MACD↓Warn")
    else:
        if pd.notna(macd_hist):
            if macd_hist < 0 and prev_macd_hist >= 0:
                score += 20; reasons.append("MACD-Cross↓")  # fresh bearish cross
            elif macd_hist < 0 and macd_hist < prev_macd_hist:
                score += 12; reasons.append("MACD↓Grow")
            elif macd_hist < 0:
                score += 6;  reasons.append("MACD↓")
            else:
                score -= 5;  reasons.append("MACD↑Warn")

    # ── Pillar 4: RSI Sweet Spot (20pts) ──────────────────────────────────────
    if direction == "LONG":
        if rsi_7 < 25:
            score -= 10; reasons.append(f"RSI-OversoldBlock({rsi_7:.0f})")  # too oversold, avoid
        elif rsi_7 < 35:
            score += 15; reasons.append(f"RSI-BounceZone({rsi_7:.0f})")     # perfect bounce entry
        elif rsi_7 < 50:
            score += 20; reasons.append(f"RSI-PullbackZone({rsi_7:.0f})")   # best entry
        elif rsi_7 < 60:
            score += 10; reasons.append(f"RSI-MidZone({rsi_7:.0f})")
        elif rsi_7 < 70:
            score += 3;  reasons.append(f"RSI-Hot({rsi_7:.0f})")
        else:
            score -= 15; reasons.append(f"RSI-OBought({rsi_7:.0f})")         # overbought, block
            return None
    else:
        if rsi_7 > 75:
            score -= 10; reasons.append(f"RSI-OverboughtBlock({rsi_7:.0f})")
        elif rsi_7 > 65:
            score += 15; reasons.append(f"RSI-BounceZone({rsi_7:.0f})")
        elif rsi_7 > 50:
            score += 20; reasons.append(f"RSI-PullbackZone({rsi_7:.0f})")
        elif rsi_7 > 40:
            score += 10; reasons.append(f"RSI-MidZone({rsi_7:.0f})")
        elif rsi_7 > 30:
            score += 3;  reasons.append(f"RSI-Hot({rsi_7:.0f})")
        else:
            score -= 15; reasons.append(f"RSI-OSold({rsi_7:.0f})")
            return None

    # ── Pillar 5: Bollinger Band Position (15pts) ──────────────────────────────
    bb_pct = last_5m["BB_PCT"]
    if pd.notna(bb_pct):
        if direction == "LONG":
            if bb_pct < 0.15:
                score += 15; reasons.append("BB-LowerEdge")   # at lower band = bounce setup
            elif bb_pct < 0.35:
                score += 10; reasons.append("BB-Lower")
            elif bb_pct < 0.65:
                score += 5;  reasons.append("BB-Mid")
            elif bb_pct > 0.85:
                score -= 8;  reasons.append("BB-UpperWarn")   # already at top
        else:
            if bb_pct > 0.85:
                score += 15; reasons.append("BB-UpperEdge")   # at upper band = short setup
            elif bb_pct > 0.65:
                score += 10; reasons.append("BB-Upper")
            elif bb_pct > 0.35:
                score += 5;  reasons.append("BB-Mid")
            elif bb_pct < 0.15:
                score -= 8;  reasons.append("BB-LowerWarn")

    # ── Pillar 6: Volume Spike (10pts) ────────────────────────────────────────
    vol_ma10 = last_5m["VOL_MA10"]
    if pd.notna(vol_ma10) and vol_ma10 > 0:
        vr = last_5m["vol"] / vol_ma10
        if vr >= 2.0:
            score += 10; reasons.append(f"VolSpike({vr:.1f}x)")   # strong confirmation
        elif vr >= 1.5:
            score += 7;  reasons.append(f"VolUp({vr:.1f}x)")
        elif vr >= 1.0:
            score += 3;  reasons.append(f"VolNorm({vr:.1f}x)")
        else:
            score -= 3;  reasons.append(f"VolLow({vr:.1f}x)")

    # ── Pillar 7: OBV Confirmation (5pts) ─────────────────────────────────────
    obv     = last_5m["OBV"]
    obv_ema = last_5m["OBV_EMA"]
    if pd.notna(obv_ema):
        if direction == "LONG" and obv > obv_ema:
            score += 5; reasons.append("OBV↑")
        elif direction == "SHORT" and obv < obv_ema:
            score += 5; reasons.append("OBV↓")

    score = max(0, score)
    if score < SCALP_THRESHOLD:
        return None

    # ── TP/SL Calculation ─────────────────────────────────────────────────────
    if direction == "LONG":
        sl   = entry - sl_atr
        tp1  = entry + sl_atr * 0.8   # quick TP1 (conservative)
        tp2  = entry + sl_atr * 1.5   # main target
        tp3  = entry + sl_atr * 2.5   # if momentum continues
        icon = "🟢"
    else:
        sl   = entry + sl_atr
        tp1  = entry - sl_atr * 0.8
        tp2  = entry - sl_atr * 1.5
        tp3  = entry - sl_atr * 2.5
        icon = "🔴"

    sl_pct_final = abs(entry - sl) / entry
    tp1_pct      = abs(tp1 - entry) / entry * 100
    tp2_pct      = abs(tp2 - entry) / entry * 100
    rr           = round(abs(tp2 - entry) / abs(sl - entry), 2)

    # Leverage — tighter SL allows higher leverage safely
    lev = min(15, max(3, round(0.02 / sl_pct_final)))

    return {
        "symbol"   : symbol.replace(":USDT",""),
        "score"    : score,
        "dir"      : f"{icon} {direction}",
        "entry"    : entry,
        "tp1"      : round(tp1, 8),
        "tp2"      : round(tp2, 8),
        "tp3"      : round(tp3, 8),
        "sl"       : round(sl, 8),
        "lev"      : lev,
        "rsi"      : round(rsi_7, 1),
        "rsi_14"   : round(rsi_14, 1),
        "atr"      : atr,
        "sl_pct"   : round(sl_pct_final * 100, 3),
        "tp1_pct"  : round(tp1_pct, 2),
        "tp2_pct"  : round(tp2_pct, 2),
        "rr"       : rr,
        "vol_24h_m": round(quote_vol / 1_000_000, 0),
        "reasons"  : " | ".join(reasons),
        "timeframe": "5m",
    }


# ─── Main Entry ───────────────────────────────────────────────────────────────

async def get_scalp_signals():
    """
    Scans top liquid pairs on 5m + 15m timeframes.
    Returns (signals, btc_price).
    Safe — uses only specific pairs, not full market scan.
    """
    exchange = ccxt.binance({
        "options"        : {"defaultType": "future"},
        "enableRateLimit": True,
    })

    try:
        await exchange.load_markets()
        logger.info(f"Scalp scan: {len(SCALP_PAIRS)} pairs on 5m+15m")

        # Get tickers for volume check
        tickers = {}
        try:
            tickers = await exchange.fetch_tickers(SCALP_PAIRS)
        except Exception as e:
            logger.warning(f"Scalp ticker fetch failed: {e}")

        # Get BTC price for context
        btc_price = 0.0
        try:
            btc_ticker = await exchange.fetch_ticker("BTC/USDT:USDT")
            btc_price  = btc_ticker.get("last") or btc_ticker.get("close") or 0.0
        except Exception:
            pass

        raw = []
        for symbol in SCALP_PAIRS:
            ticker = tickers.get(symbol, {})
            r = await analyze_scalp(exchange, symbol, ticker)
            if r:
                raw.append(r)
                logger.info(
                    f"SCALP PASS {r['symbol']:12s} {r['dir']} "
                    f"Score:{r['score']} | {r['reasons'][:50]}"
                )
            await asyncio.sleep(0.5)  # gentle rate limiting

        # Sort by score, filter by direction limits
        raw.sort(key=lambda x: x["score"], reverse=True)
        final  = []
        longs  = 0
        shorts = 0
        for sig in raw:
            is_long = "LONG" in sig["dir"]
            if is_long and longs >= SCALP_MAX_LONGS:
                continue
            if not is_long and shorts >= SCALP_MAX_SHORTS:
                continue
            if is_long:
                longs += 1
            else:
                shorts += 1
            final.append(sig)
            if len(final) >= SCALP_MAX_SIGNALS:
                break

        logger.info(f"Scalp done. Raw:{len(raw)} Final:{len(final)}")
        return final, btc_price

    finally:
        await exchange.close()
