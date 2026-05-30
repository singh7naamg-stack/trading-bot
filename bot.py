# ============================================================
#  QuestLife Signal Bot — engine.py  v7.0 CONDITION-BASED
#
#  Complete rethink. Instead of scoring 13 pillars and hoping
#  the total reaches a threshold (which fails in ranging markets),
#  this version checks specific CONDITIONS that must all be true.
#
#  LONG conditions (all must pass):
#    1. Price above 4H EMA50 (or oversold bounce with RSI < 35)
#    2. 1H EMA9 > EMA21 (short term bullish)
#    3. RSI 14 between 30-60 (not chasing)
#    4. MACD histogram positive or fresh cross
#    5. Volume not collapsing
#    6. Not extreme overbought on 4H RSI
#
#  SHORT conditions (all must pass):
#    1. Price below 4H EMA50 (confirmed bear bias)
#    2. 1H EMA9 < EMA21 (short term bearish)
#    3. RSI 14 between 40-72 (not oversold, has room to fall)
#    4. MACD histogram negative or fresh bearish cross
#    5. Volume present
#    6. Funding rate not extremely negative (shorts not overcrowded)
#
#  Quality scoring (0-100) on top of conditions to rank signals
#  Threshold: 55 — much more achievable
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

# ─── Settings ─────────────────────────────────────────────────────────────────
MIN_24H_VOLUME_USDT = 10_000_000
MAX_FUNDING_RATE    = 0.0015
MANUAL_THRESHOLD    = 55
AUTO_THRESHOLD      = 70
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
        logger.error(f"Binance 418 ban — {mins}min. Scans paused.")
    except Exception as e:
        logger.error(f"Cannot save ban file: {e}")


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
    df["EMA_21"]   = EMAIndicator(close=close, window=21).ema_indicator()
    df["EMA_50"]   = EMAIndicator(close=close, window=50).ema_indicator()
    df["EMA_200"]  = EMAIndicator(close=close, window=200).ema_indicator()
    df["RSI_14"]   = RSIIndicator(close=close, window=14).rsi()
    df["RSI_7"]    = RSIIndicator(close=close, window=7).rsi()

    _macd          = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD"]     = _macd.macd()
    df["MACD_SIG"] = _macd.macd_signal()
    df["MACD_HIST"]= _macd.macd_diff()

    df["ADX"]      = ADXIndicator(high=high, low=low, close=close, window=14).adx()
    df["ATR"]      = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()

    _bb            = BollingerBands(close=close, window=20, window_dev=2)
    df["BB_UP"]    = _bb.bollinger_hband()
    df["BB_MID"]   = _bb.bollinger_mavg()
    df["BB_LOW"]   = _bb.bollinger_lband()
    df["BB_PCT"]   = _bb.bollinger_pband()

    df["OBV"]      = OnBalanceVolumeIndicator(close=close, volume=vol).on_balance_volume()
    df["OBV_EMA"]  = EMAIndicator(close=df["OBV"], window=20).ema_indicator()
    df["VOL_MA20"] = vol.rolling(window=20).mean()

    return df


# ─── TP/SL Calculator ────────────────────────────────────────────────────────

def calc_tps(entry, atr, direction):
    if direction == "LONG":
        sl  = entry - atr * 1.5
        tp1 = entry + atr * 1.0
        tp2 = entry + atr * 2.0
        tp3 = entry + atr * 3.5
    else:
        sl  = entry + atr * 1.5
        tp1 = entry - atr * 1.0
        tp2 = entry - atr * 2.0
        tp3 = entry - atr * 3.5
    return (
        round(sl, 8),
        round(tp1, 8),
        round(tp2, 8),
        round(tp3, 8),
    )


# ─── Core Analysis ────────────────────────────────────────────────────────────

async def analyze_symbol(exchange, symbol, ticker, funding_rate, ctx):
    """
    v7.0 CONDITION-BASED analysis.

    For a signal to fire ALL core conditions must pass.
    Quality score (0-100) is used to rank signals.
    Works in trending AND ranging AND slow bleed markets.
    """
    coin      = symbol.replace("/USDT:USDT","").replace("/USDT","")
    quote_vol = ticker.get("quoteVolume") or 0

    if quote_vol < MIN_24H_VOLUME_USDT:
        return None
    if funding_rate is not None and abs(funding_rate) > MAX_FUNDING_RATE:
        return None

    # Fetch candles
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

    # Current values
    h1       = df_1h.iloc[-1]
    h1_prev  = df_1h.iloc[-2]
    h4       = df_4h.iloc[-1]
    h4_prev  = df_4h.iloc[-2]

    entry    = h1["close"]
    atr      = h1["ATR"]
    rsi_14   = h1["RSI_14"]
    rsi_7    = h1["RSI_7"]
    adx      = h1["ADX"]
    macd_h   = h1["MACD_HIST"]
    prev_mh  = h1_prev["MACD_HIST"]
    bb_pct   = h1["BB_PCT"]
    obv      = h1["OBV"]
    obv_ema  = h1["OBV_EMA"]
    vol_ma   = h1["VOL_MA20"]
    ema9_1h  = h1["EMA_9"]
    ema21_1h = h1["EMA_21"]
    ema50_1h = h1["EMA_50"]
    ema200_1h= h1["EMA_200"]

    ema9_4h  = h4["EMA_9"]
    ema21_4h = h4["EMA_21"]
    ema50_4h = h4["EMA_50"]
    rsi_4h   = h4["RSI_14"]
    close_4h = h4["close"]

    if pd.isna(atr) or atr == 0:
        return None

    # ── Determine candidate direction ─────────────────────────────────────────
    # Use 1H EMA9 vs EMA21 as primary direction indicator
    if ema9_1h > ema21_1h:
        direction = "LONG"
    elif ema9_1h < ema21_1h:
        direction = "SHORT"
    else:
        return None

    reasons = []
    passed  = []
    failed  = []

    # ══════════════════════════════════════════════════════════════════════════
    # CORE CONDITIONS — ALL must pass for signal to fire
    # ══════════════════════════════════════════════════════════════════════════

    if direction == "SHORT":
        # ── Condition 1: 4H bearish bias ──────────────────────────────────────
        # Price below 4H EMA50 OR 4H EMA50 is declining
        # This is TRUE in slow bleeds even without EMA cross
        price_below_4h_ema50 = close_4h < ema50_4h
        ema50_4h_declining   = ema50_4h < h4_prev["EMA_50"] * 1.0005
        ema9_below_ema21_4h  = ema9_4h < ema21_4h

        if price_below_4h_ema50 or ema50_4h_declining or ema9_below_ema21_4h:
            passed.append("C1-4H-Bear")
        else:
            return None  # 4H not bearish — no short

        # ── Condition 2: 1H momentum bearish ─────────────────────────────────
        # Already confirmed by EMA9 < EMA21 above
        passed.append("C2-1H-Bear")

        # ── Condition 3: RSI has room to fall ─────────────────────────────────
        # RSI must be between 35-75 — not already oversold
        if 35 <= rsi_14 <= 75:
            passed.append(f"C3-RSI({rsi_14:.0f})")
        elif rsi_14 < 35:
            return None  # already oversold — shorts would chase a bottom
        else:
            return None  # above 75 — too overbought for 1H but check if fresh

        # ── Condition 4: MACD bearish ─────────────────────────────────────────
        # Histogram negative OR just crossed bearish
        if pd.notna(macd_h):
            if macd_h < 0:
                passed.append("C4-MACD-Bear")
            elif pd.notna(prev_mh) and prev_mh > 0 and macd_h <= 0:
                passed.append("C4-MACD-FreshCross")
            else:
                failed.append("C4-MACD-Bull")
                return None  # MACD still bullish — not time to short
        else:
            return None

        # ── Condition 5: Volume present ───────────────────────────────────────
        if pd.notna(vol_ma) and vol_ma > 0:
            vol_ratio = h1["vol"] / vol_ma
            if vol_ratio >= 0.5:  # very relaxed — just needs some volume
                passed.append(f"C5-Vol({vol_ratio:.1f}x)")
            else:
                return None  # zero volume = dead coin, skip
        else:
            return None

        # ── Condition 6: Funding not overcrowded short ───────────────────────
        if funding_rate is not None and funding_rate < -0.001:
            return None  # shorts already overcrowded, dangerous
        passed.append("C6-FR-OK")

    else:  # LONG
        # ── Condition 1: 4H bullish bias ──────────────────────────────────────
        price_above_4h_ema50 = close_4h > ema50_4h
        oversold_bounce      = rsi_7 < 38 and rsi_14 < 45  # oversold bounce mode
        ema9_above_ema21_4h  = ema9_4h > ema21_4h

        if price_above_4h_ema50 or ema9_above_ema21_4h:
            passed.append("C1-4H-Bull")
        elif oversold_bounce and not ctx.btc_is_bearish():
            passed.append("C1-OversoldBounce")
        else:
            return None  # No bullish 4H bias and not oversold bounce

        # BTC gate — block altcoin LONGs in BTC bear (unless oversold bounce)
        if coin != "BTC" and ctx.btc_is_bearish() and not oversold_bounce:
            return None

        # ── Condition 2: 1H momentum bullish ─────────────────────────────────
        passed.append("C2-1H-Bull")

        # ── Condition 3: RSI not overbought ───────────────────────────────────
        if rsi_14 <= 65:
            passed.append(f"C3-RSI({rsi_14:.0f})")
        elif rsi_14 <= 70:
            passed.append(f"C3-RSI-High({rsi_14:.0f})")
        else:
            return None  # overbought — no long

        # ── Condition 4: MACD bullish ─────────────────────────────────────────
        if pd.notna(macd_h):
            if macd_h > 0:
                passed.append("C4-MACD-Bull")
            elif pd.notna(prev_mh) and prev_mh < 0 and macd_h >= 0:
                passed.append("C4-MACD-FreshCross")
            else:
                return None  # MACD bearish — don't long
        else:
            return None

        # ── Condition 5: Volume ───────────────────────────────────────────────
        if pd.notna(vol_ma) and vol_ma > 0:
            vol_ratio = h1["vol"] / vol_ma
            if vol_ratio >= 0.5:
                passed.append(f"C5-Vol({vol_ratio:.1f}x)")
            else:
                return None
        else:
            return None

        # ── Condition 6: Funding OK ───────────────────────────────────────────
        if funding_rate is not None and funding_rate > 0.001:
            return None  # longs overcrowded
        passed.append("C6-FR-OK")

    reasons.extend(passed)

    # ══════════════════════════════════════════════════════════════════════════
    # QUALITY SCORING — ranks signals that passed all conditions
    # Range: 0-100 (does NOT block signals, only ranks them)
    # ══════════════════════════════════════════════════════════════════════════

    score = 50  # base score for passing all conditions

    # EMA alignment quality (+15)
    if direction == "SHORT":
        if ema9_1h < ema21_1h < ema50_1h:
            score += 15; reasons.append("EMA-Aligned↓")
        elif ema9_1h < ema21_1h:
            score += 8;  reasons.append("EMA9<21")
    else:
        if ema9_1h > ema21_1h > ema50_1h:
            score += 15; reasons.append("EMA-Aligned↑")
        elif ema9_1h > ema21_1h:
            score += 8;  reasons.append("EMA9>21")

    # EMA200 position (+8)
    if pd.notna(ema200_1h):
        if direction == "SHORT" and entry < ema200_1h:
            score += 8; reasons.append("BelowEMA200")
        elif direction == "LONG" and entry > ema200_1h:
            score += 8; reasons.append("AboveEMA200")
        elif direction == "SHORT" and entry > ema200_1h:
            score -= 5; reasons.append("AboveEMA200!")
        elif direction == "LONG" and entry < ema200_1h:
            score -= 3; reasons.append("BelowEMA200!")

    # Fresh MACD cross bonus (+10)
    if pd.notna(macd_h) and pd.notna(prev_mh):
        if direction == "SHORT" and macd_h < 0 and prev_mh >= 0:
            score += 10; reasons.append("FreshBearCross")
        elif direction == "LONG" and macd_h > 0 and prev_mh <= 0:
            score += 10; reasons.append("FreshBullCross")

    # RSI sweet spot (+8)
    if direction == "SHORT":
        if 50 <= rsi_14 <= 65: score += 8; reasons.append(f"RSI-Sweet({rsi_14:.0f})")
        elif 40 <= rsi_14 < 50: score += 4
    else:
        if 35 <= rsi_14 <= 52: score += 8; reasons.append(f"RSI-Sweet({rsi_14:.0f})")
        elif rsi_14 < 35: score += 5; reasons.append(f"RSI-Oversold({rsi_14:.0f})")

    # ADX bonus if trend is strong (+8)
    if pd.notna(adx):
        if adx >= 25:   score += 8; reasons.append(f"ADX-Strong({adx:.0f})")
        elif adx >= 18: score += 4; reasons.append(f"ADX-OK({adx:.0f})")
        else:           reasons.append(f"ADX-Weak({adx:.0f})")

    # Volume spike (+6)
    if pd.notna(vol_ma) and vol_ma > 0:
        vr = h1["vol"] / vol_ma
        if vr >= 1.5: score += 6; reasons.append(f"VolSpike({vr:.1f}x)")
        elif vr >= 1.0: score += 3

    # BB position (+6)
    if pd.notna(bb_pct):
        if direction == "SHORT" and bb_pct > 0.75:
            score += 6; reasons.append("BB-Upper")
        elif direction == "LONG" and bb_pct < 0.25:
            score += 6; reasons.append("BB-Lower")
        elif direction == "SHORT" and bb_pct < 0.2:
            score -= 5  # already at lower band, risky to short
        elif direction == "LONG" and bb_pct > 0.8:
            score -= 5

    # OBV (+5)
    if pd.notna(obv_ema):
        if direction == "SHORT" and obv < obv_ema:
            score += 5; reasons.append("OBV↓")
        elif direction == "LONG" and obv > obv_ema:
            score += 5; reasons.append("OBV↑")
        elif direction == "SHORT" and obv > obv_ema:
            score -= 3
        elif direction == "LONG" and obv < obv_ema:
            score -= 3

    # Funding rate bonus (+6)
    if funding_rate is not None:
        fr = funding_rate
        if direction == "SHORT" and fr > 0.0002:
            score += 6; reasons.append(f"FR+({fr:.3%})")
        elif direction == "LONG" and fr < -0.0002:
            score += 6; reasons.append(f"FR-({fr:.3%})")

    # Market context bonuses
    fg = ctx.fear_greed
    ls = ctx.ls_ratio
    oi = ctx.oi_change_pct

    if direction == "SHORT":
        if fg < 30:    score += 5; reasons.append(f"Fear({fg})")  # fear = confirms downtrend
        if ls > 1.3:   score += 5; reasons.append(f"CrowdLong({ls:.2f})")
        if oi > 0.5:   score += 3; reasons.append("OI↑")
    else:
        if fg < 25:    score += 8; reasons.append(f"ExtremeFear({fg})")  # extreme fear = bounce
        if ls < 0.8:   score += 5; reasons.append(f"CrowdShort({ls:.2f})")
        if oi > 1.0:   score += 3; reasons.append("OI↑")

    # Macro penalty (reduced — only 8pts for HIGH)
    if ctx.macro_event_today:
        pen = 8 if ctx.macro_event_impact == "HIGH" else 4
        score -= pen; reasons.append(f"Macro-{pen}")

    # BTC dominance penalty for alt LONGs
    if direction == "LONG" and coin != "BTC" and ctx.btc_dominance > 57:
        score -= 4; reasons.append(f"HighDom({ctx.btc_dominance:.0f}%)")

    score = max(0, min(100, score))

    if score < MANUAL_THRESHOLD:
        return None

    # ── Build result ──────────────────────────────────────────────────────────
    sl, tp1, tp2, tp3 = calc_tps(entry, atr, direction)
    sl_pct = abs(entry - sl) / entry
    lev    = min(20, max(1, round(0.02 / sl_pct))) if sl_pct > 0 else 1
    rr     = round(abs(tp2 - entry) / abs(sl - entry), 2)
    liq_est = entry * 0.92 if direction == "LONG" else entry * 1.08
    icon    = "🟢" if direction == "LONG" else "🔴"

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
        "rsi"          : round(rsi_14, 1),
        "adx"          : round(adx, 1) if pd.notna(adx) else 0,
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


# ─── Correlation Filter ───────────────────────────────────────────────────────

def filter_correlated(signals):
    final, longs, shorts = [], 0, 0
    for sig in signals:
        is_long = "LONG" in sig["dir"]
        if is_long  and longs  >= MAX_LONGS:  continue
        if not is_long and shorts >= MAX_SHORTS: continue
        longs  += is_long
        shorts += not is_long
        final.append(sig)
        if len(final) >= MAX_SIGNALS:
            break
    return final


# ─── Main Entry ───────────────────────────────────────────────────────────────

async def get_top_signals():
    if is_banned():
        mins = get_ban_remaining_mins()
        logger.warning(f"Binance ban — {mins}min remaining. Skipping.")
        return [], MarketContext()

    token    = os.getenv("CRYPTOPANIC_TOKEN", "")
    exchange = ccxt.binance({
        "options"        : {"defaultType": "future"},
        "enableRateLimit": True,
    })

    try:
        markets     = await exchange.load_markets()
        all_futures = [s for s in markets if s.endswith("/USDT:USDT")]

        logger.info("Fetching tickers...")
        try:
            tickers = await exchange.fetch_tickers(all_futures[:100])
        except Exception as e:
            if "418" in str(e):
                match = re.search(r"banned until (\d+)", str(e))
                if match: save_ban(int(match.group(1)))
                else: save_ban(int((time.time() + 3600) * 1000))
            tickers = {}

        liquid      = [s for s in tickers if (tickers[s].get("quoteVolume") or 0) >= MIN_24H_VOLUME_USDT]
        sorted_syms = sorted(liquid, key=lambda s: tickers[s].get("quoteVolume") or 0, reverse=True)[:25]
        logger.info(f"Scanning {len(sorted_syms)} pairs")

        funding_map = {}
        try:
            fd = await exchange.fetch_funding_rates(sorted_syms)
            for sym, d in fd.items():
                funding_map[sym] = d.get("fundingRate")
        except Exception as e:
            logger.warning(f"Funding rates: {e}")

        ctx = await build_market_context(exchange, sorted_syms, token)
        logger.info(
            f"Context: BTC ${ctx.btc_price:,.0f} | "
            f"4H:{ctx.btc_trend_4h} | F&G:{ctx.fear_greed} | "
            f"L/S:{ctx.ls_ratio:.2f} | OI:{ctx.oi_change_pct:+.1f}%"
        )

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
                    logger.info(
                        f"✅ PASS {r['symbol']:12s} {r['dir']} "
                        f"Score:{r['score']} | {r['reasons'][:65]}"
                    )
                else:
                    logger.debug(f"❌ FAIL {sym}")
            except Exception as e:
                logger.debug(f"Error {sym}: {e}")
            await asyncio.sleep(0.8)

        raw.sort(key=lambda x: x["score"], reverse=True)
        final = filter_correlated(raw)
        logger.info(f"Done. Passed:{len(raw)} Final:{len(final)}")
        return final, ctx

    except Exception as e:
        err = str(e)
        if "418" in err or "banned until" in err.lower():
            match = re.search(r"banned until (\d+)", err)
            if match: save_ban(int(match.group(1)))
            else: save_ban(int((time.time() + 7200) * 1000))
        else:
            logger.error(f"get_top_signals error: {e}", exc_info=True)
        return [], MarketContext()

    finally:
        await exchange.close()
