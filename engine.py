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

MANUAL_THRESHOLD    = 55
AUTO_THRESHOLD      = 70
MAX_SIGNALS         = 5
MAX_LONGS           = 3
MAX_SHORTS          = 2
MIN_24H_VOLUME_USDT = 10_000_000
MAX_FUNDING_RATE    = 0.0015

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
        logger.error(f"Binance ban saved — {mins}min")
    except Exception as e:
        logger.error(f"save_ban: {e}")


async def fetch_ohlcv_safe(exchange, symbol, timeframe, limit):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 30:
            return None
        df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","vol"])
        return df.dropna()
    except Exception as e:
        logger.debug(f"OHLCV {symbol} {timeframe}: {e}")
        return None


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
    _m             = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD_H"]   = _m.macd_diff()
    df["ATR"]      = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
    _bb            = BollingerBands(close=close, window=20, window_dev=2)
    df["BB_PCT"]   = _bb.bollinger_pband()
    df["OBV"]      = OnBalanceVolumeIndicator(close=close, volume=vol).on_balance_volume()
    df["OBV_EMA"]  = EMAIndicator(close=df["OBV"], window=20).ema_indicator()
    df["VOL_MA20"] = vol.rolling(window=20).mean()
    df["ADX"]      = ADXIndicator(high=high, low=low, close=close, window=14).adx()
    return df


async def analyze_symbol(exchange, symbol, ticker, funding_rate, ctx):
    coin      = symbol.replace("/USDT:USDT","").replace("/USDT","")
    quote_vol = ticker.get("quoteVolume") or 0
    if quote_vol < MIN_24H_VOLUME_USDT:
        return None
    if funding_rate is not None and abs(funding_rate) > MAX_FUNDING_RATE:
        return None

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

    h1      = df_1h.iloc[-1]
    h1p     = df_1h.iloc[-2]
    h4      = df_4h.iloc[-1]
    h4p     = df_4h.iloc[-2]

    entry   = h1["close"]
    atr     = h1["ATR"]
    rsi14   = h1["RSI_14"]
    rsi7    = h1["RSI_7"]
    macd_h  = h1["MACD_H"]
    macd_hp = h1p["MACD_H"]
    bb_pct  = h1["BB_PCT"]
    obv     = h1["OBV"]
    obv_ema = h1["OBV_EMA"]
    vol_ma  = h1["VOL_MA20"]
    adx     = h1["ADX"]

    e9_1h   = h1["EMA_9"]
    e21_1h  = h1["EMA_21"]
    e50_1h  = h1["EMA_50"]
    e200_1h = h1["EMA_200"]

    e9_4h   = h4["EMA_9"]
    e21_4h  = h4["EMA_21"]
    e50_4h  = h4["EMA_50"]
    close_4h= h4["close"]

    if pd.isna(atr) or atr == 0:
        return None

    # Direction from 1H EMA9 vs EMA21
    if e9_1h > e21_1h:
        direction = "LONG"
    elif e9_1h < e21_1h:
        direction = "SHORT"
    else:
        return None

    reasons = []

    # ── Core Conditions ───────────────────────────────────────────────────────
    if direction == "SHORT":
        # C1: 4H must show bearish bias
        bear_4h = (close_4h < e50_4h) or (e50_4h < h4p["EMA_50"] * 1.0005) or (e9_4h < e21_4h)
        if not bear_4h:
            return None
        reasons.append("4H-Bear")

        # C2: RSI must have room to fall (not already oversold)
        if rsi14 < 35 or rsi14 > 78:
            return None
        reasons.append(f"RSI-OK({rsi14:.0f})")

        # C3: MACD must be bearish or fresh cross
        if pd.isna(macd_h):
            return None
        if macd_h < 0:
            reasons.append("MACD-Bear")
        elif pd.notna(macd_hp) and macd_hp > 0 and macd_h <= 0:
            reasons.append("MACD-FreshBear")
        elif macd_h < macd_hp:  # MACD declining even if still positive
            reasons.append("MACD-Declining")
        else:
            return None  # MACD rising — don't short

        # C4: Funding not overcrowded short
        if funding_rate is not None and funding_rate < -0.0008:
            return None
        reasons.append("FR-OK")

        # C5: BTC still bearish (don't short alts if BTC bouncing)
        if coin != "BTC" and ctx.btc_is_bullish():
            return None

    else:  # LONG
        # C1: 4H must show bullish bias OR oversold bounce
        bull_4h      = (close_4h > e50_4h) or (e9_4h > e21_4h)
        oversold     = rsi7 < 38 and rsi14 < 42
        if not bull_4h and not oversold:
            return None
        if oversold:
            reasons.append("OversoldBounce")
        else:
            reasons.append("4H-Bull")

        # C2: BTC gate
        if coin != "BTC" and ctx.btc_is_bearish() and not oversold:
            return None

        # C3: RSI not overbought
        if rsi14 > 68:
            return None
        reasons.append(f"RSI-OK({rsi14:.0f})")

        # C4: MACD bullish
        if pd.isna(macd_h):
            return None
        if macd_h > 0:
            reasons.append("MACD-Bull")
        elif pd.notna(macd_hp) and macd_hp < 0 and macd_h >= 0:
            reasons.append("MACD-FreshBull")
        else:
            return None  # MACD bearish

        # C5: Funding not overcrowded long
        if funding_rate is not None and funding_rate > 0.001:
            return None
        reasons.append("FR-OK")

    # ── Quality Score (0-100) ─────────────────────────────────────────────────
    score = 50  # base — passed all conditions

    # EMA alignment
    if direction == "SHORT":
        if e9_1h < e21_1h < e50_1h: score += 12; reasons.append("EMA-Aligned↓")
        else: score += 5
    else:
        if e9_1h > e21_1h > e50_1h: score += 12; reasons.append("EMA-Aligned↑")
        else: score += 5

    # EMA200
    if pd.notna(e200_1h):
        if direction == "SHORT" and entry < e200_1h: score += 6; reasons.append("BelowEMA200")
        elif direction == "LONG" and entry > e200_1h: score += 6; reasons.append("AboveEMA200")
        elif direction == "SHORT" and entry > e200_1h: score -= 4
        elif direction == "LONG" and entry < e200_1h: score -= 3

    # Fresh MACD cross
    if pd.notna(macd_h) and pd.notna(macd_hp):
        if direction == "SHORT" and macd_h < 0 and macd_hp >= 0: score += 8; reasons.append("FreshCross↓")
        elif direction == "LONG" and macd_h > 0 and macd_hp <= 0: score += 8; reasons.append("FreshCross↑")

    # RSI sweet spot
    if direction == "SHORT":
        if 48 <= rsi14 <= 65: score += 8; reasons.append(f"RSI-Sweet({rsi14:.0f})")
        elif rsi14 > 65: score += 5
    else:
        if 32 <= rsi14 <= 52: score += 8; reasons.append(f"RSI-Sweet({rsi14:.0f})")
        elif rsi14 < 32: score += 5; reasons.append(f"Oversold({rsi14:.0f})")

    # ADX bonus
    if pd.notna(adx):
        if adx >= 25: score += 8; reasons.append(f"ADX-Strong({adx:.0f})")
        elif adx >= 18: score += 4; reasons.append(f"ADX-OK({adx:.0f})")

    # Volume
    if pd.notna(vol_ma) and vol_ma > 0:
        vr = h1["vol"] / vol_ma
        if vr >= 1.5: score += 6; reasons.append(f"Vol({vr:.1f}x)")
        elif vr >= 1.0: score += 2
        elif vr < 0.5: score -= 3

    # BB position
    if pd.notna(bb_pct):
        if direction == "SHORT" and bb_pct > 0.75: score += 5; reasons.append("BB-Upper")
        elif direction == "LONG" and bb_pct < 0.25: score += 5; reasons.append("BB-Lower")
        elif direction == "SHORT" and bb_pct < 0.2: score -= 5
        elif direction == "LONG" and bb_pct > 0.8: score -= 5

    # OBV
    if pd.notna(obv_ema):
        if direction == "SHORT" and obv < obv_ema: score += 4; reasons.append("OBV↓")
        elif direction == "LONG" and obv > obv_ema: score += 4; reasons.append("OBV↑")
        elif direction == "SHORT" and obv > obv_ema: score -= 3
        elif direction == "LONG" and obv < obv_ema: score -= 3

    # Funding bonus
    if funding_rate is not None:
        fr = funding_rate
        if direction == "SHORT" and fr > 0.0002: score += 6; reasons.append(f"FR+({fr:.3%})")
        elif direction == "LONG" and fr < -0.0002: score += 6; reasons.append(f"FR-({fr:.3%})")

    # Market context
    fg = ctx.fear_greed
    ls = ctx.ls_ratio
    if direction == "SHORT":
        if fg < 30: score += 5; reasons.append(f"Fear({fg})")
        if ls > 1.3: score += 4; reasons.append(f"CrowdLong({ls:.2f})")
    else:
        if fg < 25: score += 8; reasons.append(f"ExtremeFear({fg})")
        elif fg < 40: score += 3
        if ls < 0.8: score += 4; reasons.append(f"CrowdShort({ls:.2f})")

    # Macro penalty (reduced)
    if ctx.macro_event_today:
        pen = 8 if ctx.macro_event_impact == "HIGH" else 4
        score -= pen; reasons.append(f"Macro-{pen}")

    score = max(0, min(100, score))
    if score < MANUAL_THRESHOLD:
        return None

    # ── Build output ──────────────────────────────────────────────────────────
    if direction == "LONG":
        sl = entry - atr * 1.5; icon = "🟢"
        tp1 = entry + atr * 1.0
        tp2 = entry + atr * 2.0
        tp3 = entry + atr * 3.5
        liq = entry * 0.92
    else:
        sl = entry + atr * 1.5; icon = "🔴"
        tp1 = entry - atr * 1.0
        tp2 = entry - atr * 2.0
        tp3 = entry - atr * 3.5
        liq = entry * 1.08

    sl_pct = abs(entry - sl) / entry
    lev    = min(20, max(1, round(0.02 / sl_pct))) if sl_pct > 0 else 1
    rr     = round(abs(tp2 - entry) / abs(sl - entry), 2)

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
        "rsi"          : round(rsi14, 1),
        "adx"          : round(adx, 1) if pd.notna(adx) else 0,
        "rr"           : rr,
        "atr"          : atr,
        "funding_rate" : round(funding_rate * 100, 4) if funding_rate is not None else None,
        "vol_24h_m"    : round(quote_vol / 1_000_000, 1),
        "news"         : ctx.news_sentiment.get(coin, "NEUTRAL"),
        "news_headline": ctx.news_headlines.get(coin, ""),
        "liq_est"      : round(liq, 6),
        "sl_pct"       : round(sl_pct * 100, 2),
        "reasons"      : " | ".join(reasons),
    }


def filter_signals(signals):
    final, longs, shorts = [], 0, 0
    for s in signals:
        is_long = "LONG" in s["dir"]
        if is_long and longs >= MAX_LONGS: continue
        if not is_long and shorts >= MAX_SHORTS: continue
        longs  += is_long
        shorts += not is_long
        final.append(s)
        if len(final) >= MAX_SIGNALS: break
    return final


async def get_top_signals():
    if is_banned():
        logger.warning(f"Ban active — {get_ban_remaining_mins()}min left")
        return [], MarketContext()

    token    = os.getenv("CRYPTOPANIC_TOKEN", "")
    exchange = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})

    try:
        markets     = await exchange.load_markets()
        all_futures = [s for s in markets if s.endswith("/USDT:USDT")]

        try:
            tickers = await exchange.fetch_tickers(all_futures[:100])
        except Exception as e:
            err = str(e)
            if "418" in err:
                m = re.search(r"banned until (\d+)", err)
                save_ban(int(m.group(1)) if m else int((time.time()+3600)*1000))
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
            logger.warning(f"Funding: {e}")

        ctx = await build_market_context(exchange, sorted_syms, token)

        raw = []
        for sym in sorted_syms:
            try:
                r = await analyze_symbol(exchange, sym, tickers.get(sym,{}), funding_map.get(sym), ctx)
                if r:
                    raw.append(r)
                    logger.info(f"PASS {r['symbol']:12s} {r['dir']} {r['score']}pts | {r['reasons'][:60]}")
            except Exception as e:
                logger.debug(f"{sym}: {e}")
            await asyncio.sleep(0.8)

        raw.sort(key=lambda x: x["score"], reverse=True)
        final = filter_signals(raw)
        logger.info(f"Done. Passed:{len(raw)} Final:{len(final)}")
        return final, ctx

    except Exception as e:
        err = str(e)
        if "418" in err:
            m = re.search(r"banned until (\d+)", err)
            save_ban(int(m.group(1)) if m else int((time.time()+7200)*1000))
        else:
            logger.error(f"get_top_signals: {e}", exc_info=True)
        return [], MarketContext()

    finally:
        await exchange.close()
