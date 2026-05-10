# ============================================================
#  QuestLife Signal Bot — market_intel.py  v5.1
#  Fixed: BTC price N/A bug, better fallbacks, cleaner fetches
# ============================================================

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp
import ccxt.async_support as ccxt

logger  = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=8)


# ─── Market Context ───────────────────────────────────────────────────────────

@dataclass
class MarketContext:
    btc_price          : float = 0.0
    btc_trend_4h       : str   = "NEUTRAL"
    btc_trend_daily    : str   = "NEUTRAL"
    btc_change_24h     : float = 0.0
    fear_greed         : int   = 50
    fear_greed_label   : str   = "Neutral"
    btc_dominance      : float = 0.0
    ls_ratio           : float = 1.0
    oi_change_pct      : float = 0.0
    macro_event_today  : bool  = False
    macro_event_name   : str   = ""
    macro_event_impact : str   = ""
    news_sentiment     : dict  = field(default_factory=dict)
    news_headlines     : dict  = field(default_factory=dict)
    fetched_at         : str   = ""
    warnings           : list  = field(default_factory=list)

    def btc_is_bullish(self):   return self.btc_trend_4h == "BULL"
    def btc_is_bearish(self):   return self.btc_trend_4h == "BEAR"
    def is_extreme_fear(self):  return self.fear_greed < 25
    def is_extreme_greed(self): return self.fear_greed > 75


# ─── BTC Price + Trend ────────────────────────────────────────────────────────

async def fetch_btc_price_direct() -> float:
    """
    Fetch BTC price directly from Binance REST — single lightweight call.
    Used as primary price source and fallback when candles fail.
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(
                "https://fapi.binance.com/fapi/v1/ticker/price",
                params={"symbol": "BTCUSDT"}
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return float(data["price"])
    except Exception as e:
        logger.warning(f"BTC direct price failed: {e}")
    return 0.0


async def fetch_btc_24h_change() -> float:
    """Fetch BTC 24h percentage change from Binance futures ticker."""
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(
                "https://fapi.binance.com/fapi/v1/ticker/24hr",
                params={"symbol": "BTCUSDT"}
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return float(data.get("priceChangePercent", 0))
    except Exception as e:
        logger.warning(f"BTC 24h change failed: {e}")
    return 0.0


async def fetch_btc_trend(exchange) -> dict:
    """
    Fetch BTC 4H and daily trend using EMA 20/50 crossover.
    Gets price from direct REST call first (most reliable),
    then candle close as confirmation.

    WHY BTC TREND MATTERS:
      80% of altcoins follow BTC direction.
      BTC 4H bearish = block all altcoin LONG signals.
      This single filter eliminates the most losing trades.
    """
    result = {
        "price"      : 0.0,
        "trend_4h"   : "NEUTRAL",
        "trend_daily": "NEUTRAL",
        "change_24h" : 0.0,
    }

    try:
        import pandas as pd
        from ta.trend import EMAIndicator

        # Fetch price directly + candles concurrently
        price_task    = fetch_btc_price_direct()
        change_task   = fetch_btc_24h_change()
        candles_4h_t  = exchange.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=60)
        candles_1d_t  = exchange.fetch_ohlcv("BTC/USDT:USDT", "1d", limit=60)

        price, change_24h, candles_4h, candles_daily = await asyncio.gather(
            price_task, change_task, candles_4h_t, candles_1d_t,
            return_exceptions=True
        )

        # Use direct price as primary — most reliable
        if isinstance(price, float) and price > 0:
            result["price"] = price
        
        if isinstance(change_24h, float):
            result["change_24h"] = change_24h

        def trend_from_candles(candles):
            """Return (trend_str, last_close) from OHLCV candle list."""
            if not isinstance(candles, list) or len(candles) < 50:
                return "NEUTRAL", 0.0
            try:
                close = pd.Series([float(c[4]) for c in candles])
                ema20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
                ema50 = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
                last  = float(close.iloc[-1])

                # Fallback price from candle if direct fetch failed
                if result["price"] == 0 and last > 0:
                    result["price"] = last

                if ema20 > ema50 * 1.001:   return "BULL", last
                elif ema20 < ema50 * 0.999: return "BEAR", last
                return "NEUTRAL", last
            except Exception as e:
                logger.debug(f"trend_from_candles error: {e}")
                return "NEUTRAL", 0.0

        trend_4h,    _ = trend_from_candles(candles_4h)
        trend_daily, _ = trend_from_candles(candles_daily)

        result["trend_4h"]    = trend_4h
        result["trend_daily"] = trend_daily

        logger.info(
            f"BTC ${result['price']:,.0f} | "
            f"4H:{trend_4h} | Daily:{trend_daily} | "
            f"24h:{result['change_24h']:+.2f}%"
        )

    except Exception as e:
        logger.warning(f"BTC trend fetch failed: {e}")
        # Last resort — try direct price only
        if result["price"] == 0:
            result["price"] = await fetch_btc_price_direct()

    return result


# ─── Fear & Greed ─────────────────────────────────────────────────────────────

async def fetch_fear_and_greed() -> dict:
    """
    Crypto Fear & Greed Index from alternative.me (free, no API key).
    0-24 Extreme Fear | 25-49 Fear | 50-74 Greed | 75-100 Extreme Greed
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.alternative.me/fng/?limit=1") as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    d    = data["data"][0]
                    val  = int(d["value"])
                    label = d["value_classification"]
                    logger.info(f"Fear & Greed: {val} ({label})")
                    return {"value": val, "label": label}
    except Exception as e:
        logger.warning(f"Fear & Greed failed: {e}")
    return {"value": 50, "label": "Neutral"}


# ─── BTC Dominance ────────────────────────────────────────────────────────────

async def fetch_btc_dominance() -> float:
    """
    BTC dominance % from CoinGecko free API.
    Above 55% = capital in BTC, bad for altcoin LONGs.
    Below 45% = alt season, great for altcoin LONGs.
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.coingecko.com/api/v3/global") as r:
                if r.status == 200:
                    data = await r.json()
                    dom  = round(data["data"]["market_cap_percentage"].get("btc", 0), 2)
                    logger.info(f"BTC dominance: {dom:.1f}%")
                    return dom
    except Exception as e:
        logger.warning(f"BTC dominance failed: {e}")
    return 0.0


# ─── Long/Short Ratio ─────────────────────────────────────────────────────────

async def fetch_long_short_ratio(exchange) -> float:
    """
    BTC global long/short ratio from Binance.
    > 1.5 = crowd heavily long  → contrarian short signal
    < 0.7 = crowd heavily short → contrarian long signal (squeeze likely)
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": "BTCUSDT", "period": "4h", "limit": 1}
            ) as r:
                if r.status == 200:
                    data  = await r.json()
                    ratio = float(data[0]["longShortRatio"])
                    logger.info(f"L/S ratio: {ratio:.3f}")
                    return ratio
    except Exception as e:
        logger.warning(f"Long/Short ratio failed: {e}")
    return 1.0


# ─── Open Interest Change ─────────────────────────────────────────────────────

async def fetch_open_interest_change(exchange) -> float:
    """
    BTC open interest % change over last 4H from Binance.
    OI rising + price up  = real buyers entering → strong LONG
    OI rising + price down = shorts entering    → strong SHORT
    OI falling            = unwinding, trend may reverse
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": "BTCUSDT", "period": "4h", "limit": 2}
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if len(data) >= 2:
                        old    = float(data[0]["sumOpenInterestValue"])
                        new    = float(data[1]["sumOpenInterestValue"])
                        change = round(((new - old) / old) * 100 if old else 0, 2)
                        logger.info(f"OI change: {change:+.2f}%")
                        return change
    except Exception as e:
        logger.warning(f"OI change failed: {e}")
    return 0.0


# ─── Macro Events ─────────────────────────────────────────────────────────────

async def fetch_macro_events() -> dict:
    """
    High-impact USD economic events from ForexFactory public JSON.
    Checks for FOMC, CPI, NFP, Fed speeches, interest rate decisions today.

    FOMC day = expect 3-10% crypto swings. Bot applies -20pt penalty.
    Best practice: reduce size or skip trading on FOMC/CPI days.
    """
    HIGH_IMPACT_KEYWORDS = [
        "FOMC", "Federal Reserve", "Fed Chair", "Interest Rate",
        "CPI", "Consumer Price", "Inflation",
        "Non-Farm", "NFP", "Employment",
        "GDP", "Jerome Powell", "Fed Meeting",
        "ETF", "Bitcoin ETF",
    ]
    result = {"event": False, "name": "", "impact": ""}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json") as r:
                if r.status == 200:
                    events = await r.json(content_type=None)
                    for ev in events:
                        if ev.get("country") != "USD":                  continue
                        if ev.get("date", "")[:10] != today:             continue
                        if ev.get("impact") not in ("High", "Medium"):  continue
                        title  = ev.get("title", "")
                        impact = "HIGH" if ev["impact"] == "High" else "MEDIUM"
                        for kw in HIGH_IMPACT_KEYWORDS:
                            if kw.lower() in title.lower():
                                logger.warning(f"MACRO EVENT: {title} [{impact}]")
                                return {"event": True, "name": title, "impact": impact}
                        if ev["impact"] == "High":
                            logger.warning(f"HIGH USD EVENT: {title}")
                            return {"event": True, "name": title, "impact": "HIGH"}
    except Exception as e:
        logger.warning(f"Macro events failed: {e}")
    return result


# ─── Crypto News ──────────────────────────────────────────────────────────────

async def fetch_crypto_news(coins: list, api_token: str = "") -> tuple:
    """
    CryptoPanic news sentiment — free tier.
    Register at cryptopanic.com/developers/api/ for a free token.
    Add CRYPTOPANIC_TOKEN to Render env vars to enable.

    Positive news on a coin = +8pts bonus on LONG signals.
    Negative news (hack, SEC, exploit) = -10pts, signal may be blocked.
    """
    if not api_token:
        return {}, {}

    sentiment_map = {}
    headline_map  = {}

    try:
        for i in range(0, min(len(coins), 20), 5):
            batch = ",".join(coins[i:i+5])
            url   = (
                f"https://cryptopanic.com/api/free/v1/posts/"
                f"?auth_token={api_token}&currencies={batch}"
                f"&filter=important&public=true"
            )
            async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()

            for post in data.get("results", []):
                currencies = [c["code"] for c in post.get("currencies", [])]
                votes      = post.get("votes", {})
                pos        = (votes.get("positive") or 0) + (votes.get("liked") or 0)
                neg        = (votes.get("negative") or 0) + (votes.get("disliked") or 0)
                imp        = votes.get("important") or 0
                title      = post.get("title", "")[:80]
                weight     = 2 if imp > 3 else 1

                for coin in currencies:
                    cu = coin.upper()
                    if cu not in coins:
                        continue
                    if cu not in sentiment_map:
                        sentiment_map[cu] = 0
                        headline_map[cu]  = title
                    sentiment_map[cu] += (pos - neg) * weight

            await asyncio.sleep(0.5)

    except Exception as e:
        logger.warning(f"CryptoPanic failed: {e}")

    # Convert raw scores to labels
    final = {}
    for coin, score in sentiment_map.items():
        final[coin] = "POSITIVE" if score > 3 else ("NEGATIVE" if score < -3 else "NEUTRAL")

    return final, headline_map


# ─── Main Assembler ───────────────────────────────────────────────────────────

async def build_market_context(
    exchange,
    scan_symbols      : list,
    cryptopanic_token : str = "",
) -> MarketContext:
    """
    Assemble ALL market intelligence in parallel — called ONCE per scan.
    Results shared across all symbol analyses in engine.py.

    Parallel fetch dramatically reduces total scan time vs sequential.
    """
    ctx            = MarketContext()
    ctx.fetched_at = datetime.now(timezone.utc).strftime("%H:%M UTC")
    coin_names     = list({
        s.replace("/USDT:USDT", "").replace("/USDT", "")
        for s in scan_symbols
    })

    logger.info("Building market context (parallel fetch)...")

    # All fetches run concurrently — total time = slowest single fetch
    results = await asyncio.gather(
        fetch_btc_trend(exchange),
        fetch_fear_and_greed(),
        fetch_btc_dominance(),
        fetch_long_short_ratio(exchange),
        fetch_open_interest_change(exchange),
        fetch_macro_events(),
        fetch_crypto_news(coin_names, cryptopanic_token),
        return_exceptions=True,
    )

    btc_data, fg_data, btc_dom, ls_ratio, oi_change, macro_data, news_data = results

    # BTC trend + price (with fallback)
    if isinstance(btc_data, dict):
        ctx.btc_price       = btc_data.get("price", 0.0)
        ctx.btc_trend_4h    = btc_data.get("trend_4h", "NEUTRAL")
        ctx.btc_trend_daily = btc_data.get("trend_daily", "NEUTRAL")
        ctx.btc_change_24h  = btc_data.get("change_24h", 0.0)

    # If BTC price still 0 after all that, do one final direct fetch
    if ctx.btc_price == 0:
        logger.warning("BTC price still 0 after context build — trying direct fetch")
        ctx.btc_price = await fetch_btc_price_direct()

    # Fear & Greed
    if isinstance(fg_data, dict):
        ctx.fear_greed       = fg_data.get("value", 50)
        ctx.fear_greed_label = fg_data.get("label", "Neutral")

    # BTC dominance
    if isinstance(btc_dom, float) and btc_dom > 0:
        ctx.btc_dominance = btc_dom

    # Long/Short ratio
    if isinstance(ls_ratio, float) and ls_ratio > 0:
        ctx.ls_ratio = ls_ratio

    # OI change
    if isinstance(oi_change, float):
        ctx.oi_change_pct = oi_change

    # Macro events
    if isinstance(macro_data, dict) and macro_data.get("event"):
        ctx.macro_event_today  = True
        ctx.macro_event_name   = macro_data.get("name", "")
        ctx.macro_event_impact = macro_data.get("impact", "HIGH")
        ctx.warnings.append(f"MACRO: {ctx.macro_event_name}")

    # News sentiment
    if isinstance(news_data, tuple) and len(news_data) == 2:
        ctx.news_sentiment = news_data[0]
        ctx.news_headlines = news_data[1]

    logger.info(
        f"Context ready | "
        f"BTC ${ctx.btc_price:,.0f} {ctx.btc_trend_4h} | "
        f"F&G:{ctx.fear_greed} {ctx.fear_greed_label} | "
        f"Dom:{ctx.btc_dominance:.1f}% | "
        f"L/S:{ctx.ls_ratio:.2f} | "
        f"OI:{ctx.oi_change_pct:+.1f}% | "
        f"Macro:{ctx.macro_event_today}"
    )
    return ctx
