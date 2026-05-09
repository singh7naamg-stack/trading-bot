# ============================================================
#  QuestLife Signal Bot — engine.py  v4.0
#  Full market-aware signal engine with 8-pillar scoring
# ============================================================
#
#  NEW IN v4.0:
#    - Uses MarketContext from market_intel.py
#    - BTC 4H bearish = all LONG signals BLOCKED (global filter)
#    - Macro event today = score penalty -20pts (never trade FOMC day)
#    - News sentiment = ±8 pts per coin
#    - OI + L/S ratio = new Pillar 6 (replaces funding-only)
#    - BTC dominance filter for alt LONG signals
#    - All v3.0 filters retained (volume, funding, ATR guards)
#
#  SCORING SYSTEM (120 pts max):
#    Pillar 1 — 1H EMA Trend        : 25 pts
#    Pillar 2 — 4H MTF Confirmation : 20 pts
#    Pillar 3 — RSI Momentum        : 25 pts
#    Pillar 4 — ADX Trend Strength  : 15 pts
#    Pillar 5 — Volume Conviction   : 10 pts
#    Pillar 6 — Funding + OI + L/S  : 15 pts
#    Pillar 7 — Fear & Greed        :  5 pts
#    Pillar 8 — News Sentiment      :  8 pts  (bonus/penalty)
#    Macro penalty                  : -20 pts (FOMC/CPI day)
# ============================================================

import asyncio
import logging
import os

import pandas as pd
import ccxt.async_support as ccxt

from ta.momentum   import RSIIndicator
from ta.trend      import EMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange

from market_intel import MarketContext, build_market_context

logger = logging.getLogger(__name__)

# ─── Thresholds ───────────────────────────────────────────────────────────────
MIN_24H_VOLUME_USDT = 5_000_000   # $5M minimum 24H volume
MAX_FUNDING_RATE    = 0.0015      # ±0.15% — above this = squeeze zone, skip
MANUAL_THRESHOLD    = 60
AUTO_THRESHOLD      = 75


# ─── OHLCV & Indicators ──────────────────────────────────────────────────────

async def fetch_ohlcv_safe(exchange, symbol: str, timeframe: str, limit: int):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 50:
            return None
        df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])
        return df.dropna()
    except Exception as e:
        logger.debug(f"OHLCV failed [{symbol} {timeframe}]: {e}")
        return None


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df    = df.copy()
    close = df["close"]
    df["EMA_20"]    = EMAIndicator(close=close, window=20).ema_indicator()
    df["EMA_50"]    = EMAIndicator(close=close, window=50).ema_indicator()
    df["RSI_14"]    = RSIIndicator(close=close, window=14).rsi()
    df["ATR_14"]    = AverageTrueRange(high=df["high"], low=df["low"], close=close, window=14).average_true_range()
    df["ADX_14"]    = ADXIndicator(high=df["high"], low=df["low"], close=close, window=14).adx()
    df["VOL_MA_20"] = df["vol"].rolling(window=20).mean()
    return df


# ─── Per-Symbol Analysis ──────────────────────────────────────────────────────

async def analyze_symbol(
    exchange     : object,
    symbol       : str,
    ticker       : dict,
    funding_rate : float | None,
    ctx          : MarketContext,
) -> dict | None:
    """
    Full 8-pillar signal analysis enriched with global MarketContext.

    Global filters applied BEFORE any indicator calculation:
      1. Min $5M 24H volume
      2. BTC 4H bearish + signal is LONG = blocked
      3. Extreme funding rate (±0.15%)
      4. Macro event = heavy penalty
    """
    coin = symbol.replace("/USDT:USDT", "").replace("/USDT", "")

    # ══════════════════════════════════════════════════════════════════════
    #  GLOBAL FILTER 1 — Minimum Liquidity
    # ══════════════════════════════════════════════════════════════════════
    quote_vol = ticker.get("quoteVolume") or 0
    if quote_vol < MIN_24H_VOLUME_USDT:
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  GLOBAL FILTER 2 — Extreme Funding Rate Guard
    # ══════════════════════════════════════════════════════════════════════
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
    if len(df_1h) < 3 or len(df_4h) < 3:
        return None

    last = df_1h.iloc[-1]
    l4h  = df_4h.iloc[-1]

    score     = 0
    direction = None
    reasons   = []
    warnings  = []

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 1 — 1H EMA Trend (25 pts)
    # ══════════════════════════════════════════════════════════════════════
    if last["EMA_20"] > last["EMA_50"]:
        score    += 25
        direction = "LONG"
        reasons.append("1H EMA↑")
    elif last["EMA_20"] < last["EMA_50"]:
        score    += 25
        direction = "SHORT"
        reasons.append("1H EMA↓")
    else:
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  GLOBAL FILTER 3 — BTC 4H Direction Gate
    #  If BTC 4H is bearish, block ALL altcoin LONG signals.
    #  Altcoins follow BTC ~80% of the time. Fighting this loses money.
    #  Exception: if coin IS BTC, skip this check.
    # ══════════════════════════════════════════════════════════════════════
    if coin != "BTC" and direction == "LONG" and ctx.btc_is_bearish():
        logger.debug(f"BLOCKED {symbol} LONG — BTC 4H is BEARISH")
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 2 — 4H MTF Confirmation (20 pts)
    # ══════════════════════════════════════════════════════════════════════
    if direction == "LONG" and l4h["EMA_20"] > l4h["EMA_50"]:
        score += 20
        reasons.append("4H↑")
    elif direction == "SHORT" and l4h["EMA_20"] < l4h["EMA_50"]:
        score += 20
        reasons.append("4H↓")

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 3 — RSI Momentum & Entry Timing (25 pts)
    # ══════════════════════════════════════════════════════════════════════
    rsi = last["RSI_14"]
    if direction == "LONG":
        if 35 <= rsi <= 52:    score += 25; reasons.append(f"RSI Pullback({rsi:.0f})")
        elif rsi < 35:         score += 12; reasons.append(f"RSI Oversold({rsi:.0f})")
        elif rsi <= 62:        score += 8;  reasons.append(f"RSI Mid({rsi:.0f})")
    elif direction == "SHORT":
        if 48 <= rsi <= 65:    score += 25; reasons.append(f"RSI Bounce({rsi:.0f})")
        elif rsi > 65:         score += 12; reasons.append(f"RSI OB({rsi:.0f})")
        elif rsi >= 38:        score += 8;  reasons.append(f"RSI Mid({rsi:.0f})")

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 4 — ADX Trend Strength (15 pts)
    # ══════════════════════════════════════════════════════════════════════
    adx = last["ADX_14"]
    if adx >= 35:    score += 15; reasons.append(f"ADX Strong({adx:.0f})")
    elif adx >= 25:  score += 8;  reasons.append(f"ADX OK({adx:.0f})")
    elif adx >= 20:  score += 3;  reasons.append(f"ADX Weak({adx:.0f})")

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 5 — Volume Conviction (10 pts)
    # ══════════════════════════════════════════════════════════════════════
    vol    = last["vol"]
    vol_ma = last["VOL_MA_20"]
    if pd.notna(vol_ma) and vol_ma > 0:
        vr = vol / vol_ma
        if vr >= 1.5:   score += 10; reasons.append(f"VolSurge({vr:.1f}x)")
        elif vr >= 1.0: score += 5;  reasons.append(f"Vol OK({vr:.1f}x)")

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 6 — Funding Rate + OI + Long/Short Ratio (15 pts)
    #
    #  Three futures-specific data points combined:
    #    Funding rate — 8H crowd positioning fee
    #    OI change    — real money entering or leaving
    #    L/S ratio    — how retail is positioned (contrarian signal)
    # ══════════════════════════════════════════════════════════════════════
    futures_pts = 0

    # Funding rate contribution (max 8 pts)
    if funding_rate is not None:
        fr = funding_rate
        if direction == "LONG":
            if fr < -0.0001:              futures_pts += 8; reasons.append(f"FR Bull({fr:.3%})")
            elif abs(fr) <= 0.0005:       futures_pts += 4; reasons.append(f"FR OK({fr:.3%})")
        elif direction == "SHORT":
            if fr > 0.0001:               futures_pts += 8; reasons.append(f"FR Bear({fr:.3%})")
            elif abs(fr) <= 0.0005:       futures_pts += 4; reasons.append(f"FR OK({fr:.3%})")

    # OI change contribution (max 4 pts)
    # Rising OI confirms the move is real (new money entering, not just shorts covering)
    if ctx.oi_change_pct != 0:
        if ctx.oi_change_pct > 1.0:    futures_pts += 4; reasons.append(f"OI↑({ctx.oi_change_pct:+.1f}%)")
        elif ctx.oi_change_pct > 0:    futures_pts += 2; reasons.append(f"OI slight↑")
        elif ctx.oi_change_pct < -1.0: futures_pts -= 3; reasons.append(f"OI↓({ctx.oi_change_pct:+.1f}%)")

    # L/S ratio contribution (max 3 pts, contrarian)
    # Crowd is usually wrong at extremes — bet against them
    ls = ctx.ls_ratio
    if direction == "LONG" and ls < 0.8:    # Crowd heavily short → long squeeze likely
        futures_pts += 3; reasons.append(f"L/S Contrarian({ls:.2f})")
    elif direction == "SHORT" and ls > 1.5: # Crowd heavily long → short squeeze likely
        futures_pts += 3; reasons.append(f"L/S Contrarian({ls:.2f})")

    score += min(futures_pts, 15)  # Cap pillar at 15 pts

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 7 — Fear & Greed Macro Sentiment (5 pts / -5 penalty)
    # ══════════════════════════════════════════════════════════════════════
    fg = ctx.fear_greed
    if direction == "LONG":
        if fg < 25:          score += 5;  reasons.append(f"ExtFear(FG:{fg})")
        elif fg < 50:        score += 2;  reasons.append(f"Fear(FG:{fg})")
        elif fg > 75:        score -= 5;  warnings.append(f"ExtremeGreed(FG:{fg})")
    elif direction == "SHORT":
        if fg > 75:          score += 5;  reasons.append(f"ExtGreed(FG:{fg})")
        elif fg >= 50:       score += 2;  reasons.append(f"Greed(FG:{fg})")
        elif fg < 25:        score -= 5;  warnings.append(f"ExtremeFear(FG:{fg})")

    # ══════════════════════════════════════════════════════════════════════
    #  PILLAR 8 — News Sentiment (±8 pts)
    #  If CryptoPanic token configured: bonus for positive, block for negative
    # ══════════════════════════════════════════════════════════════════════
    news = ctx.news_sentiment.get(coin, "NEUTRAL")
    if news == "POSITIVE" and direction == "LONG":
        score += 8
        reasons.append(f"NewsPositive")
    elif news == "POSITIVE" and direction == "SHORT":
        score -= 5  # Don't short a coin with good news
        warnings.append("NewsContra")
    elif news == "NEGATIVE" and direction == "LONG":
        score -= 10
        warnings.append("NegativeNews!")
        if score < 50:  # If news is very bad, skip entirely
            logger.info(f"SKIP {symbol}: negative news drags score too low")
            return None
    elif news == "NEGATIVE" and direction == "SHORT":
        score += 8
        reasons.append("NegNewsShort")

    # ══════════════════════════════════════════════════════════════════════
    #  MACRO EVENT PENALTY — FOMC / CPI / NFP day (−20 pts)
    #  These events cause unpredictable 5-10% swings.
    #  On FOMC day, even strong signals fail because news overrides TA.
    #  We don't block entirely — but score drops enough to filter most signals.
    # ══════════════════════════════════════════════════════════════════════
    if ctx.macro_event_today:
        if ctx.macro_event_impact == "HIGH":
            score -= 20
            warnings.append(f"MACRO:{ctx.macro_event_name[:20]}")
        else:
            score -= 10
            warnings.append(f"MACRO_MED:{ctx.macro_event_name[:20]}")

    # BTC dominance penalty for alt LONGs when dominance is rising
    if direction == "LONG" and coin != "BTC" and ctx.btc_dominance > 55:
        score -= 5
        warnings.append(f"BTCDom>{ctx.btc_dominance:.0f}%")

    score = max(0, score)

    if score < MANUAL_THRESHOLD:
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  RISK MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════
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

    sl_pct = abs(entry - sl) / entry
    lev    = min(20, max(1, round(0.02 / sl_pct))) if sl_pct > 0 else 1
    rr     = round(abs(tp - entry) / abs(sl - entry), 2)

    all_reasons = " | ".join(reasons)
    if warnings:
        all_reasons += " ⚠️ " + " | ".join(warnings)

    news_headline = ctx.news_headlines.get(coin, "")

    return {
        "symbol"        : symbol.replace(":USDT", ""),
        "score"         : score,
        "dir"           : f"{icon} {direction}",
        "entry"         : entry,
        "tp"            : tp,
        "sl"            : sl,
        "lev"           : lev,
        "rsi"           : round(rsi, 1),
        "adx"           : round(adx, 1),
        "rr"            : rr,
        "funding_rate"  : round(funding_rate * 100, 4) if funding_rate is not None else None,
        "vol_24h_m"     : round(quote_vol / 1_000_000, 1),
        "news"          : news,
        "news_headline" : news_headline,
        "reasons"       : all_reasons,
    }


# ─── Main Entry Point ─────────────────────────────────────────────────────────

async def get_top_signals() -> tuple[list[dict], MarketContext]:
    """
    Full v4.0 pipeline:
      1. Connect Binance Futures
      2. Load markets → liquidity filter → top 40 by volume
      3. Fetch funding rates (batch)
      4. Build full MarketContext (BTC trend, F&G, dominance, L/S, OI, macro, news)
      5. Analyze each symbol with global context
      6. Return (signals, context) — context used for /briefing command
    """
    cryptopanic_token = os.getenv("CRYPTOPANIC_TOKEN", "")

    exchange = ccxt.binance({
        "options"        : {"defaultType": "future"},
        "enableRateLimit": True,
    })

    try:
        markets = await exchange.load_markets()
        all_futures = [s for s in markets if s.endswith("/USDT:USDT")]

        # ── Liquidity filter ──────────────────────────────────────────────
        logger.info("Fetching tickers for liquidity filter...")
        try:
            tickers = await exchange.fetch_tickers(all_futures)
        except Exception:
            tickers = {}

        liquid = [s for s in tickers if (tickers[s].get("quoteVolume") or 0) >= MIN_24H_VOLUME_USDT]
        sorted_syms = sorted(liquid, key=lambda s: tickers[s].get("quoteVolume") or 0, reverse=True)[:40]

        logger.info(f"Scanning {len(sorted_syms)} liquid pairs | Top 5: {[s.replace('/USDT:USDT','') for s in sorted_syms[:5]]}")

        # ── Funding rates (batch) ─────────────────────────────────────────
        funding_map: dict = {}
        try:
            fd_data = await exchange.fetch_funding_rates(sorted_syms)
            for sym, fd in fd_data.items():
                funding_map[sym] = fd.get("fundingRate")
        except Exception as e:
            logger.warning(f"Funding rates failed: {e}")

        # ── Build full market context ─────────────────────────────────────
        ctx = await build_market_context(
            exchange          = exchange,
            scan_symbols      = sorted_syms,
            cryptopanic_token = cryptopanic_token,
        )

        # ── Analyze each symbol ───────────────────────────────────────────
        signals = []
        for symbol in sorted_syms:
            result = await analyze_symbol(
                exchange     = exchange,
                symbol       = symbol,
                ticker       = tickers.get(symbol, {}),
                funding_rate = funding_map.get(symbol),
                ctx          = ctx,
            )
            if result:
                signals.append(result)
                logger.info(f"SIGNAL {result['symbol']:18s} {result['dir']} Score:{result['score']} | {result['reasons'][:60]}")
            await asyncio.sleep(0.3)

        logger.info(f"Scan complete. {len(signals)} signal(s) from {len(sorted_syms)} pairs.")
        return sorted(signals, key=lambda x: x["score"], reverse=True), ctx

    finally:
        await exchange.close()
