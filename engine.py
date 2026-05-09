# ============================================================
#  QuestLife Signal Bot — engine.py  v5.0  STRICT MODE
#  Support/Resistance | Candle Patterns | Correlation Filter
# ============================================================
#
#  PHILOSOPHY v5.0:
#    Quality over quantity. Maximum 5 signals per scan.
#    If nothing is genuinely powerful, sends nothing.
#    Every signal must pass 6 hard filters before scoring.
#    Designed to give you 2-5 high conviction trades per day,
#    not 15 mediocre ones.
#
#  HARD FILTERS (coin rejected instantly if any fail):
#    1. Min $10M 24H volume        (up from $5M — stricter)
#    2. BTC 4H gate                (bearish BTC = no LONG)
#    3. Extreme funding (>0.15%)   (squeeze zone — skip)
#    4. ADX must be >= 22          (no ranging markets)
#    5. 4H MTF must confirm        (counter-trend = rejected)
#    6. No chasing entries         (RSI>68 LONG or RSI<32 SHORT)
#
#  SCORING SYSTEM (130 pts max):
#    Pillar 1 — 1H EMA Trend        : 25 pts
#    Pillar 2 — 4H MTF Confirmation : 20 pts
#    Pillar 3 — RSI Momentum        : 25 pts
#    Pillar 4 — ADX Strength        : 15 pts
#    Pillar 5 — Volume Conviction   : 10 pts
#    Pillar 6 — Funding + OI + L/S  : 15 pts
#    Pillar 7 — Fear & Greed        :  5 pts
#    Pillar 8 — News Sentiment      :  8 pts
#    Pillar 9 — Support/Resistance  : 10 pts  NEW
#    Pillar 10— Candle Pattern      : 10 pts  NEW
#    Macro penalty                  : -20 pts
#    S/R blocking penalty           : -15 pts
#
#  THRESHOLDS (much stricter than v4):
#    Manual /signals : >= 75%  (was 60%)
#    Auto alerts     : >= 85%  (was 75%)
#    Max signals     : 5       (was unlimited)
#    Max LONGs       : 3       (correlation filter)
#    Max SHORTs      : 2       (correlation filter)
# ============================================================

import asyncio
import logging
import os

import pandas as pd
import ccxt.async_support as ccxt

from ta.momentum   import RSIIndicator
from ta.trend      import EMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange

from market_intel  import MarketContext, build_market_context

logger = logging.getLogger(__name__)

# ─── Strict Thresholds ────────────────────────────────────────────────────────
MIN_24H_VOLUME_USDT  = 10_000_000  # $10M minimum — stricter than v4
MAX_FUNDING_RATE     = 0.0015      # 0.15% extreme funding guard
MIN_ADX              = 22          # Hard block ranging markets
MANUAL_THRESHOLD     = 75          # Very strict manual
AUTO_THRESHOLD       = 85          # Extremely strict auto
MAX_SIGNALS          = 5           # Never more than 5 signals
MAX_LONGS            = 3           # Correlation cap
MAX_SHORTS           = 2           # Correlation cap


# ─── OHLCV Fetch ─────────────────────────────────────────────────────────────

async def fetch_ohlcv_safe(exchange, symbol, timeframe, limit):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 50:
            return None
        df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])
        return df.dropna()
    except Exception as e:
        logger.debug(f"OHLCV failed [{symbol} {timeframe}]: {e}")
        return None


# ─── Indicators ──────────────────────────────────────────────────────────────

def add_indicators(df):
    df    = df.copy()
    close = df["close"]
    df["EMA_20"]    = EMAIndicator(close=close, window=20).ema_indicator()
    df["EMA_50"]    = EMAIndicator(close=close, window=50).ema_indicator()
    df["RSI_14"]    = RSIIndicator(close=close, window=14).rsi()
    df["ATR_14"]    = AverageTrueRange(high=df["high"], low=df["low"], close=close, window=14).average_true_range()
    df["ADX_14"]    = ADXIndicator(high=df["high"], low=df["low"], close=close, window=14).adx()
    df["VOL_MA_20"] = df["vol"].rolling(window=20).mean()
    return df


# ─── NEW: Support & Resistance Detection ─────────────────────────────────────

def find_sr_levels(df, swing_window=3):
    """
    Find swing highs (resistance) and swing lows (support) from candle data.
    A swing high = higher than N candles on each side.
    A swing low  = lower than N candles on each side.
    Uses last 60 candles for relevance.

    WHY THIS MATTERS:
      If your TP is at $3.85 but there's a massive resistance wall at $3.70,
      the price will likely reject there and never reach TP.
      This catches that problem before you enter.
    """
    df = df.tail(60).reset_index(drop=True)
    resistances = []
    supports    = []
    n = swing_window

    for i in range(n, len(df) - n):
        # Swing high: this candle's high is highest in the window
        if all(df["high"].iloc[i] >= df["high"].iloc[i - j] for j in range(1, n + 1)) and \
           all(df["high"].iloc[i] >= df["high"].iloc[i + j] for j in range(1, n + 1)):
            resistances.append(df["high"].iloc[i])

        # Swing low: this candle's low is lowest in the window
        if all(df["low"].iloc[i] <= df["low"].iloc[i - j] for j in range(1, n + 1)) and \
           all(df["low"].iloc[i] <= df["low"].iloc[i + j] for j in range(1, n + 1)):
            supports.append(df["low"].iloc[i])

    return sorted(set(resistances)), sorted(set(supports))


def check_sr_score(entry, tp, sl, direction, resistances, supports):
    """
    Score a signal based on whether TP path is clear of S/R levels.
    Returns (score_pts, reason_str).

    Rules:
      LONG:  resistance between entry and TP = penalty (blocked path)
             entry near strong support = bonus (good base)
             path totally clear = bonus
      SHORT: support between entry and TP = penalty (blocked path)
             entry near strong resistance = bonus (good ceiling)
    """
    tolerance = 0.015  # 1.5% proximity = "near" a level

    if direction == "LONG":
        # Check for resistance blocking the path to TP
        blockers = [r for r in resistances if entry * 1.005 < r < tp]
        if blockers:
            nearest = min(blockers)
            block_pct = (nearest - entry) / (tp - entry)
            if block_pct < 0.4:
                return -15, f"HardBlock-R@{nearest:.4g}"   # Resistance very close — major penalty
            elif block_pct < 0.7:
                return -8,  f"SoftBlock-R@{nearest:.4g}"   # Resistance mid-way — minor penalty
            else:
                return -3,  f"FarBlock-R@{nearest:.4g}"    # Resistance near TP — tiny penalty

        # Entry near strong support = excellent base
        near_sup = [s for s in supports if abs(entry - s) / entry < tolerance]
        if near_sup:
            return 10, "AtSupport"

        # Path completely clear
        return 5, "PathClear"

    elif direction == "SHORT":
        # Check for support blocking the path to TP
        blockers = [s for s in supports if tp < s < entry * 0.995]
        if blockers:
            nearest = max(blockers)
            block_pct = (entry - nearest) / (entry - tp)
            if block_pct < 0.4:
                return -15, f"HardBlock-S@{nearest:.4g}"
            elif block_pct < 0.7:
                return -8,  f"SoftBlock-S@{nearest:.4g}"
            else:
                return -3,  f"FarBlock-S@{nearest:.4g}"

        near_res = [r for r in resistances if abs(entry - r) / entry < tolerance]
        if near_res:
            return 10, "AtResistance"

        return 5, "PathClear"

    return 0, ""


# ─── NEW: Candle Pattern Recognition ─────────────────────────────────────────

def detect_candle_pattern(df, direction):
    """
    Detect the last 1-2 candle patterns to confirm or deny the signal.
    Returns (score_pts, pattern_name).

    WHY THIS MATTERS:
      A 91% score LONG signal on a bearish engulfing candle is a losing trade.
      The bot scores indicators but was blind to the actual candle shape.
      This pillar adds pattern awareness.

    Patterns detected:
      Bullish engulfing  = strong LONG confirmation
      Bearish engulfing  = strong SHORT confirmation
      Hammer             = bullish reversal
      Shooting star      = bearish reversal
      Doji               = indecision — penalty on any signal
      Strong body candle = trend continuation confirmation
      Opposite pattern   = penalty (signal fights the candle)
    """
    if len(df) < 3:
        return 0, ""

    last = df.iloc[-1]
    prev = df.iloc[-2]

    body      = abs(last["close"] - last["open"])
    rng       = last["high"] - last["low"]
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    if rng < 0.0000001:
        return 0, ""

    body_ratio = body / rng
    is_bull    = last["close"] > last["open"]
    is_bear    = last["close"] < last["open"]

    # ── Doji: indecision candle — bad for any directional signal ─────────
    if body_ratio < 0.08:
        return -8, "Doji-Indecision"

    # ── Bullish Engulfing: last candle fully engulfs previous bearish ─────
    if (is_bull and
        prev["close"] < prev["open"] and
        last["close"] > prev["open"] and
        last["open"] < prev["close"]):
        if direction == "LONG":
            return 10, "BullEngulf"
        else:
            return -10, "BullEngulf-Contra"

    # ── Bearish Engulfing: last candle fully engulfs previous bullish ─────
    if (is_bear and
        prev["close"] > prev["open"] and
        last["close"] < prev["open"] and
        last["open"] > prev["close"]):
        if direction == "SHORT":
            return 10, "BearEngulf"
        else:
            return -10, "BearEngulf-Contra"

    # ── Hammer: long lower wick, small body — bullish reversal ───────────
    if (lower_wick > body * 2.0 and
        upper_wick < body * 0.5 and
        body_ratio > 0.1):
        if direction == "LONG":
            return 8, "Hammer"
        else:
            return -6, "Hammer-Contra"

    # ── Shooting Star: long upper wick — bearish reversal ────────────────
    if (upper_wick > body * 2.0 and
        lower_wick < body * 0.5 and
        body_ratio > 0.1):
        if direction == "SHORT":
            return 8, "ShootingStar"
        else:
            return -6, "ShootingStar-Contra"

    # ── Strong body candle in signal direction — continuation ─────────────
    if body_ratio > 0.65:
        if is_bull and direction == "LONG":
            return 5, "StrongBullCandle"
        elif is_bear and direction == "SHORT":
            return 5, "StrongBearCandle"
        elif is_bull and direction == "SHORT":
            return -5, "BullCandle-Contra"
        elif is_bear and direction == "LONG":
            return -5, "BearCandle-Contra"

    return 0, "NeutralCandle"


# ─── Correlation Filter ───────────────────────────────────────────────────────

def filter_correlated(signals):
    """
    Enforce strict limits to avoid correlated positions.

    Problem: if bot gives 8 LONG signals in one scan, you're not
    getting 8 independent trades — you're making the same BTC-correlated
    bet 8 times. When BTC dumps, all 8 lose simultaneously.

    Solution: max 3 LONGs, max 2 SHORTs, max 5 total.
    Within each direction, keep only the highest scoring coins.
    """
    final        = []
    long_count   = 0
    short_count  = 0

    for sig in signals:  # already sorted by score desc
        is_long = "LONG" in sig["dir"]

        if is_long:
            if long_count >= MAX_LONGS:
                continue
            long_count += 1
        else:
            if short_count >= MAX_SHORTS:
                continue
            short_count += 1

        final.append(sig)
        if len(final) >= MAX_SIGNALS:
            break

    return final


# ─── Per-Symbol Analysis ──────────────────────────────────────────────────────

async def analyze_symbol(exchange, symbol, ticker, funding_rate, ctx):
    coin      = symbol.replace("/USDT:USDT", "").replace("/USDT", "")
    quote_vol = ticker.get("quoteVolume") or 0

    # ══ HARD FILTER 1: Liquidity ($10M+) ═════════════════════════════════
    if quote_vol < MIN_24H_VOLUME_USDT:
        return None

    # ══ HARD FILTER 2: Extreme Funding ═══════════════════════════════════
    if funding_rate is not None and abs(funding_rate) > MAX_FUNDING_RATE:
        return None

    # ── Fetch candles ─────────────────────────────────────────────────────
    df_1h, df_4h = await asyncio.gather(
        fetch_ohlcv_safe(exchange, symbol, "1h", 100),
        fetch_ohlcv_safe(exchange, symbol, "4h", 100),
    )
    if df_1h is None or df_4h is None:
        return None

    df_1h = add_indicators(df_1h).dropna()
    df_4h = add_indicators(df_4h).dropna()
    if len(df_1h) < 5 or len(df_4h) < 3:
        return None

    last = df_1h.iloc[-1]
    l4h  = df_4h.iloc[-1]

    # ══ HARD FILTER 3: ADX >= 22 (no ranging markets) ════════════════════
    adx = last["ADX_14"]
    if adx < MIN_ADX:
        return None

    # ══ PILLAR 1: 1H EMA Trend (25 pts) ══════════════════════════════════
    score     = 0
    direction = None
    reasons   = []

    if last["EMA_20"] > last["EMA_50"]:
        score += 25; direction = "LONG";  reasons.append("1H EMA↑")
    elif last["EMA_20"] < last["EMA_50"]:
        score += 25; direction = "SHORT"; reasons.append("1H EMA↓")
    else:
        return None

    # ══ HARD FILTER 4: BTC gate (LONG blocked if BTC 4H bearish) ═════════
    if coin != "BTC" and direction == "LONG" and ctx.btc_is_bearish():
        return None

    # ══ HARD FILTER 5: 4H MTF must confirm — no exceptions in v5 ═════════
    # v4 gave partial score without 4H confirmation. v5 rejects entirely.
    mtf_bull = l4h["EMA_20"] > l4h["EMA_50"]
    mtf_bear = l4h["EMA_20"] < l4h["EMA_50"]

    if direction == "LONG" and not mtf_bull:
        return None   # 1H bullish but 4H bearish = counter-trend, skip
    if direction == "SHORT" and not mtf_bear:
        return None   # 1H bearish but 4H bullish = counter-trend, skip

    score += 20
    reasons.append("4H Confirmed")

    # ══ HARD FILTER 6: No chasing entries ════════════════════════════════
    rsi = last["RSI_14"]
    if direction == "LONG"  and rsi > 68:
        return None   # Overbought — chasing a move already done
    if direction == "SHORT" and rsi < 32:
        return None   # Oversold — chasing a move already done

    # ══ PILLAR 3: RSI Timing (25 pts) ════════════════════════════════════
    if direction == "LONG":
        if 35 <= rsi <= 52:   score += 25; reasons.append(f"RSI Pullback({rsi:.0f})")
        elif rsi < 35:        score += 12; reasons.append(f"RSI Oversold({rsi:.0f})")
        elif rsi <= 62:       score += 8;  reasons.append(f"RSI Mid({rsi:.0f})")
    else:
        if 48 <= rsi <= 65:   score += 25; reasons.append(f"RSI Bounce({rsi:.0f})")
        elif rsi > 65:        score += 12; reasons.append(f"RSI OB({rsi:.0f})")
        elif rsi >= 38:       score += 8;  reasons.append(f"RSI Mid({rsi:.0f})")

    # ══ PILLAR 4: ADX Strength (15 pts) ══════════════════════════════════
    if adx >= 35:    score += 15; reasons.append(f"ADX Strong({adx:.0f})")
    elif adx >= 28:  score += 10; reasons.append(f"ADX Good({adx:.0f})")
    elif adx >= 22:  score += 5;  reasons.append(f"ADX OK({adx:.0f})")

    # ══ PILLAR 5: Volume (10 pts) ═════════════════════════════════════════
    vol    = last["vol"]
    vol_ma = last["VOL_MA_20"]
    if pd.notna(vol_ma) and vol_ma > 0:
        vr = vol / vol_ma
        if vr >= 1.5:   score += 10; reasons.append(f"VolSurge({vr:.1f}x)")
        elif vr >= 1.0: score += 5;  reasons.append(f"Vol OK({vr:.1f}x)")

    # ══ PILLAR 6: Funding + OI + L/S (15 pts) ════════════════════════════
    fp = 0
    if funding_rate is not None:
        fr = funding_rate
        if direction == "LONG":
            if fr < -0.0001:          fp += 8; reasons.append(f"FR Bull({fr:.3%})")
            elif abs(fr) <= 0.0005:   fp += 4; reasons.append(f"FR OK")
        else:
            if fr > 0.0001:           fp += 8; reasons.append(f"FR Bear({fr:.3%})")
            elif abs(fr) <= 0.0005:   fp += 4; reasons.append(f"FR OK")

    if ctx.oi_change_pct > 1.0:   fp += 4; reasons.append(f"OI↑{ctx.oi_change_pct:.1f}%")
    elif ctx.oi_change_pct > 0:   fp += 2; reasons.append("OI slight↑")
    elif ctx.oi_change_pct < -1:  fp -= 3; reasons.append(f"OI↓{ctx.oi_change_pct:.1f}%")

    ls = ctx.ls_ratio
    if direction == "LONG"  and ls < 0.8:  fp += 3; reasons.append(f"L/S Contra({ls:.2f})")
    if direction == "SHORT" and ls > 1.5:  fp += 3; reasons.append(f"L/S Contra({ls:.2f})")

    score += min(fp, 15)

    # ══ PILLAR 7: Fear & Greed (5 pts) ═══════════════════════════════════
    fg = ctx.fear_greed
    if direction == "LONG":
        if fg < 25:     score += 5; reasons.append(f"ExtFear(FG:{fg})")
        elif fg < 50:   score += 2; reasons.append(f"Fear(FG:{fg})")
        elif fg > 75:   score -= 5; reasons.append(f"GreedWarn(FG:{fg})")
    else:
        if fg > 75:     score += 5; reasons.append(f"ExtGreed(FG:{fg})")
        elif fg >= 50:  score += 2; reasons.append(f"Greed(FG:{fg})")
        elif fg < 25:   score -= 5; reasons.append(f"FearWarn(FG:{fg})")

    # ══ PILLAR 8: News (8 pts) ═══════════════════════════════════════════
    news = ctx.news_sentiment.get(coin, "NEUTRAL")
    if news == "POSITIVE" and direction == "LONG":
        score += 8;  reasons.append("NewsPos")
    elif news == "POSITIVE" and direction == "SHORT":
        score -= 5;  reasons.append("NewsContra")
    elif news == "NEGATIVE" and direction == "LONG":
        score -= 10; reasons.append("NewsNeg!")
        if score < 60:
            return None
    elif news == "NEGATIVE" and direction == "SHORT":
        score += 8;  reasons.append("NegNewsShort")

    # ══ MACRO PENALTY ════════════════════════════════════════════════════
    if ctx.macro_event_today:
        penalty = 20 if ctx.macro_event_impact == "HIGH" else 10
        score  -= penalty
        reasons.append(f"MacroPenalty-{penalty}")

    # BTC dominance penalty for alt LONGs
    if direction == "LONG" and coin != "BTC" and ctx.btc_dominance > 55:
        score -= 5; reasons.append(f"BTC.Dom>{ctx.btc_dominance:.0f}%")

    score = max(0, score)

    # Early exit if already below threshold before S/R and candle checks
    if score < MANUAL_THRESHOLD - 20:
        return None

    # ══ Risk Management ═══════════════════════════════════════════════════
    entry = last["close"]
    atr   = last["ATR_14"]
    if pd.isna(atr) or atr == 0 or atr < entry * 0.0001:
        return None

    if direction == "LONG":
        sl = entry - (atr * 1.5);  tp = entry + (atr * 3.0);  icon = "🟢"
    else:
        sl = entry + (atr * 1.5);  tp = entry - (atr * 3.0);  icon = "🔴"

    if abs(tp - entry) < entry * 0.001 or abs(sl - entry) < entry * 0.0001:
        return None

    # ══ PILLAR 9: Support & Resistance (10 pts or -15 penalty) ═══════════
    resistances, supports = find_sr_levels(df_1h)
    sr_pts, sr_reason     = check_sr_score(entry, tp, sl, direction, resistances, supports)
    score += sr_pts
    if sr_reason:
        reasons.append(sr_reason)

    # ══ PILLAR 10: Candle Pattern (10 pts or -10 penalty) ════════════════
    candle_pts, candle_reason = detect_candle_pattern(df_1h, direction)
    score += candle_pts
    if candle_reason:
        reasons.append(candle_reason)

    score = max(0, score)

    # ══ Final strict threshold check ═════════════════════════════════════
    if score < MANUAL_THRESHOLD:
        return None

    sl_pct = abs(entry - sl) / entry
    lev    = min(20, max(1, round(0.02 / sl_pct))) if sl_pct > 0 else 1
    rr     = round(abs(tp - entry) / abs(sl - entry), 2)

    return {
        "symbol"      : symbol.replace(":USDT", ""),
        "score"       : score,
        "dir"         : f"{icon} {direction}",
        "entry"       : entry,
        "tp"          : tp,
        "sl"          : sl,
        "lev"         : lev,
        "rsi"         : round(rsi, 1),
        "adx"         : round(adx, 1),
        "rr"          : rr,
        "funding_rate": round(funding_rate * 100, 4) if funding_rate is not None else None,
        "vol_24h_m"   : round(quote_vol / 1_000_000, 1),
        "news"        : news,
        "news_headline": ctx.news_headlines.get(coin, ""),
        "reasons"     : " | ".join(reasons),
    }


# ─── Main Entry Point ─────────────────────────────────────────────────────────

async def get_top_signals():
    """
    v5.0 strict pipeline:
      Returns (signals_list, market_context)
      signals_list is capped at MAX_SIGNALS (5) after correlation filter.
      Returns empty list with context if nothing meets strict criteria.
    """
    cryptopanic_token = os.getenv("CRYPTOPANIC_TOKEN", "")

    exchange = ccxt.binance({
        "options"        : {"defaultType": "future"},
        "enableRateLimit": True,
    })

    try:
        markets     = await exchange.load_markets()
        all_futures = [s for s in markets if s.endswith("/USDT:USDT")]

        # ── Liquidity filter + sort by volume ────────────────────────────
        logger.info("Fetching tickers...")
        try:
            tickers = await exchange.fetch_tickers(all_futures)
        except Exception:
            tickers = {}

        liquid      = [s for s in tickers if (tickers[s].get("quoteVolume") or 0) >= MIN_24H_VOLUME_USDT]
        sorted_syms = sorted(liquid, key=lambda s: tickers[s].get("quoteVolume") or 0, reverse=True)[:40]

        logger.info(f"Scanning {len(sorted_syms)} pairs | Top 5: {[s.replace('/USDT:USDT','') for s in sorted_syms[:5]]}")

        # ── Funding rates (batch) ─────────────────────────────────────────
        funding_map = {}
        try:
            fd = await exchange.fetch_funding_rates(sorted_syms)
            for sym, data in fd.items():
                funding_map[sym] = data.get("fundingRate")
        except Exception as e:
            logger.warning(f"Funding rates failed: {e}")

        # ── Market context ────────────────────────────────────────────────
        ctx = await build_market_context(exchange, sorted_syms, cryptopanic_token)

        # ── Analyze each symbol ───────────────────────────────────────────
        raw_signals = []
        for symbol in sorted_syms:
            result = await analyze_symbol(
                exchange     = exchange,
                symbol       = symbol,
                ticker       = tickers.get(symbol, {}),
                funding_rate = funding_map.get(symbol),
                ctx          = ctx,
            )
            if result:
                raw_signals.append(result)
                logger.info(f"PASS {result['symbol']:18s} {result['dir']} Score:{result['score']} | {result['reasons'][:70]}")
            await asyncio.sleep(0.3)

        # Sort by score, then apply strict correlation filter
        raw_signals.sort(key=lambda x: x["score"], reverse=True)
        final_signals = filter_correlated(raw_signals)

        logger.info(
            f"Scan done. Raw: {len(raw_signals)} | After correlation filter: {len(final_signals)} | "
            f"Thresholds: manual={MANUAL_THRESHOLD}% auto={AUTO_THRESHOLD}%"
        )
        return final_signals, ctx

    finally:
        await exchange.close()
