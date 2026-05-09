# ============================================================
#  QuestLife Signal Bot — engine.py  v2.0
#  5-Pillar Analysis Engine | Multi-Timeframe | Volume-Sorted
# ============================================================

import ccxt.async_support as ccxt
import pandas as pd
import asyncio
import logging

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange

logger = logging.getLogger(__name__)


# ─── Data Fetching ────────────────────────────────────────────────────────────

async def fetch_ohlcv_safe(exchange, symbol: str, timeframe: str, limit: int):
    """Fetch OHLCV candles safely, returns DataFrame or None."""
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 50:
            return None
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        df = df.dropna()
        return df
    except Exception as e:
        logger.debug(f"OHLCV fetch failed [{symbol} {timeframe}]: {e}")
        return None


# ─── Indicator Calculation ────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to a OHLCV dataframe."""
    df = df.copy()
    close = df['close']

    df['EMA_20']   = EMAIndicator(close=close, window=20).ema_indicator()
    df['EMA_50']   = EMAIndicator(close=close, window=50).ema_indicator()
    df['RSI_14']   = RSIIndicator(close=close, window=14).rsi()
    df['ATR_14']   = AverageTrueRange(
                         high=df['high'], low=df['low'],
                         close=close, window=14
                     ).average_true_range()
    df['ADX_14']   = ADXIndicator(
                         high=df['high'], low=df['low'],
                         close=close, window=14
                     ).adx()
    df['VOL_MA_20'] = df['vol'].rolling(window=20).mean()

    return df


# ─── Per-Symbol Analysis ──────────────────────────────────────────────────────

async def analyze_symbol(exchange, symbol: str) -> dict | None:
    """
    Full multi-timeframe 5-pillar analysis for one symbol.
    Returns a signal dict on success, None if no valid signal.

    SCORING SYSTEM (100 pts max):
      Pillar 1 — Trend Alignment   : 25 pts  (1H EMA 20/50)
      Pillar 2 — MTF Confirmation  : 20 pts  (4H EMA 20/50 agrees)
      Pillar 3 — RSI Momentum      : 25 pts  (pullback into value zone)
      Pillar 4 — ADX Trend Strength: 15 pts  (trending vs ranging)
      Pillar 5 — Volume Conviction : 15 pts  (above avg = real move)
    """

    # Fetch 1H and 4H concurrently to save time
    df_1h, df_4h = await asyncio.gather(
        fetch_ohlcv_safe(exchange, symbol, '1h', 100),
        fetch_ohlcv_safe(exchange, symbol, '4h', 100),
    )

    if df_1h is None or df_4h is None:
        return None

    df_1h = add_indicators(df_1h)
    df_4h = add_indicators(df_4h)

    # Drop NaN rows after indicators calculated
    df_1h = df_1h.dropna()
    df_4h = df_4h.dropna()

    if len(df_1h) < 3 or len(df_4h) < 3:
        return None

    last  = df_1h.iloc[-1]
    l4h   = df_4h.iloc[-1]

    score     = 0
    direction = None
    reasons   = []

    # ── PILLAR 1: Trend Alignment via EMA 20/50 (25 pts) ─────────────────
    if last['EMA_20'] > last['EMA_50']:
        score     += 25
        direction  = "LONG"
        reasons.append("1H EMA Bullish Cross")
    elif last['EMA_20'] < last['EMA_50']:
        score     += 25
        direction  = "SHORT"
        reasons.append("1H EMA Bearish Cross")
    else:
        return None  # EMAs equal — no trend, no signal

    # ── PILLAR 2: Multi-Timeframe Confirmation (20 pts) ───────────────────
    mtf_bullish = l4h['EMA_20'] > l4h['EMA_50']
    mtf_bearish = l4h['EMA_20'] < l4h['EMA_50']

    if direction == "LONG" and mtf_bullish:
        score += 20
        reasons.append("4H Confirms Uptrend")
    elif direction == "SHORT" and mtf_bearish:
        score += 20
        reasons.append("4H Confirms Downtrend")
    # If 4H disagrees with 1H: no MTF points → weaker signal

    # ── PILLAR 3: RSI Momentum & Timing (25 pts) ──────────────────────────
    rsi = last['RSI_14']

    if direction == "LONG":
        if 35 <= rsi <= 52:          # Sweet spot: pullback in uptrend
            score += 25
            reasons.append(f"RSI Pullback {rsi:.1f}")
        elif rsi < 35:               # Oversold — valid but riskier
            score += 12
            reasons.append(f"RSI Oversold {rsi:.1f}")
        elif 52 < rsi <= 60:         # Slight momentum fade
            score += 8
            reasons.append(f"RSI Neutral {rsi:.1f}")
        # RSI > 60 in a LONG = chasing, no points

    elif direction == "SHORT":
        if 48 <= rsi <= 65:          # Sweet spot: bounce in downtrend
            score += 25
            reasons.append(f"RSI Bounce {rsi:.1f}")
        elif rsi > 65:               # Overbought — valid but riskier
            score += 12
            reasons.append(f"RSI Overbought {rsi:.1f}")
        elif 40 <= rsi < 48:
            score += 8
            reasons.append(f"RSI Neutral {rsi:.1f}")
        # RSI < 40 in a SHORT = chasing, no points

    # ── PILLAR 4: Trend Strength via ADX (15 pts) ─────────────────────────
    adx = last['ADX_14']

    if adx >= 35:
        score += 15
        reasons.append(f"Strong Trend ADX {adx:.1f}")
    elif adx >= 25:
        score += 8
        reasons.append(f"Trending ADX {adx:.1f}")
    # ADX < 25 = ranging/choppy market, skip points

    # ── PILLAR 5: Volume Conviction (15 pts) ──────────────────────────────
    vol    = last['vol']
    vol_ma = last['VOL_MA_20']

    if pd.notna(vol_ma) and vol_ma > 0:
        if vol >= vol_ma * 1.5:
            score += 15
            reasons.append("Strong Volume Surge")
        elif vol >= vol_ma:
            score += 8
            reasons.append("Above-Avg Volume")

    # ── Below threshold → not a signal ────────────────────────────────────
    if score < 60:
        return None

    # ── Risk Management ───────────────────────────────────────────────────
    entry = last['close']
    atr   = last['ATR_14']

    # Zero-ATR guard — eliminates illiquid/broken pairs like NULS, WAVES
    if pd.isna(atr) or atr < entry * 0.0001 or atr == 0:
        return None

    if direction == "LONG":
        sl   = entry - (atr * 1.5)
        tp   = entry + (atr * 3.0)
        icon = "🟢"
    else:
        sl   = entry + (atr * 1.5)
        tp   = entry - (atr * 3.0)
        icon = "🔴"

    # Validate TP/SL spread is meaningful
    if abs(tp - entry) < entry * 0.001 or abs(sl - entry) < entry * 0.0001:
        return None

    # Leverage: volatility-adjusted, 2% risk per trade, capped at 20x
    sl_pct = abs(entry - sl) / entry
    lev    = min(20, max(1, round(0.02 / sl_pct))) if sl_pct > 0 else 1

    # Risk-reward ratio
    rr = round(abs(tp - entry) / abs(sl - entry), 2)

    # Clean symbol name for display (BTC/USDT:USDT → BTC/USDT)
    display_symbol = symbol.replace(':USDT', '')

    return {
        "symbol" : display_symbol,
        "score"  : score,
        "dir"    : f"{icon} {direction}",
        "entry"  : entry,
        "tp"     : tp,
        "sl"     : sl,
        "lev"    : lev,
        "rsi"    : round(rsi, 1),
        "adx"    : round(adx, 1),
        "rr"     : rr,
        "reasons": " | ".join(reasons),
    }


# ─── Main Entry Point ─────────────────────────────────────────────────────────

async def get_top_signals() -> list[dict]:
    """
    1. Connect to Binance Futures
    2. Load top 30 USDT pairs sorted by 24h VOLUME (not alphabet)
    3. Run 5-pillar analysis on each
    4. Return signals sorted by score descending
    """
    exchange = ccxt.binance({
        'options'        : {'defaultType': 'future'},
        'enableRateLimit': True,
    })

    try:
        markets = await exchange.load_markets()

        # ✅ Correct futures filter — perpetuals have ':USDT' suffix in ccxt
        futures_syms = [s for s in markets if s.endswith('/USDT:USDT')]

        # ✅ Sort by 24h quote volume — top liquid pairs only (BTC, ETH, SOL etc.)
        logger.info(f"Fetching tickers for {len(futures_syms)} futures pairs...")
        try:
            tickers = await exchange.fetch_tickers(futures_syms)
            sorted_syms = sorted(
                [s for s in tickers if tickers[s].get('quoteVolume') is not None],
                key=lambda s: tickers[s]['quoteVolume'] or 0,
                reverse=True
            )[:30]
            logger.info(f"Top 5 by volume: {[s.replace('/USDT:USDT','') for s in sorted_syms[:5]]}")
        except Exception as e:
            logger.warning(f"Ticker sort failed, using fallback: {e}")
            sorted_syms = futures_syms[:30]

        # ✅ Analyze each symbol with rate-limit delay
        signals = []
        for symbol in sorted_syms:
            result = await analyze_symbol(exchange, symbol)
            if result:
                signals.append(result)
                logger.info(f"Signal found: {result['symbol']} {result['dir']} Score:{result['score']}")
            await asyncio.sleep(0.3)  # Prevent 418 Too Many Requests

        logger.info(f"Scan complete. {len(signals)} signal(s) found from {len(sorted_syms)} pairs.")
        return sorted(signals, key=lambda x: x['score'], reverse=True)

    finally:
        await exchange.close()
