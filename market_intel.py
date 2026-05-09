# ============================================================
#  QuestLife Signal Bot — market_intel.py  v4.0
#  Free market intelligence layer — assembled once per scan
# ============================================================
#
#  DATA SOURCES (all free, no paid API required):
#    1. Binance API       — BTC price, 4H trend, daily candle, OI, L/S ratio
#    2. alternative.me   — Fear & Greed Index
#    3. CoinGecko free   — BTC dominance %
#    4. CryptoPanic free — crypto news sentiment (free API key needed)
#    5. ForexFactory JSON — FOMC, CPI, NFP, Fed speeches economic calendar
#
#  HOW IT IMPROVES SIGNALS:
#    - BTC 4H bearish   → blocks ALL altcoin LONG signals (biggest filter)
#    - Macro event today → reduces score by 20pts (never trade FOMC day)
#    - News positive     → +8 pts bonus on mentioned coin
#    - News negative     → -10 pts penalty + signal blocked if severe
#    - BTC dominance ↑  → alt season ending, lowers LONG confidence
#    - L/S ratio extreme → crowd too one-sided, contrarian adjustment
#    - OI rising + price → real conviction move, score bonus
# ============================================================

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp
import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=8)


# ─── Market Context Dataclass ─────────────────────────────────────────────────

@dataclass
class MarketContext:
    """
    Single object passed to every signal analysis.
    Assembled once per scan so we don't hammer APIs on each coin.
    """
    # BTC trend
    btc_price        : float  = 0.0
    btc_trend_4h     : str    = "NEUTRAL"   # "BULL", "BEAR", "NEUTRAL"
    btc_trend_daily  : str    = "NEUTRAL"
    btc_change_24h   : float  = 0.0         # % change in 24h

    # Market sentiment
    fear_greed       : int    = 50          # 0-100
    fear_greed_label : str    = "Neutral"
    btc_dominance    : float  = 0.0         # % e.g. 52.3

    # Futures positioning
    ls_ratio         : float  = 1.0         # long/short ratio — >1 = more longs
    oi_change_pct    : float  = 0.0         # open interest % change vs prior period

    # Macro events
    macro_event_today   : bool = False
    macro_event_name    : str  = ""
    macro_event_impact  : str  = ""         # "HIGH", "MEDIUM"

    # News sentiment per coin (coin_symbol → "POSITIVE"/"NEGATIVE"/"NEUTRAL")
    news_sentiment   : dict   = field(default_factory=dict)
    news_headlines   : dict   = field(default_factory=dict)  # coin → headline str

    # Metadata
    fetched_at       : str    = ""
    warnings         : list   = field(default_factory=list)

    def btc_is_bullish(self) -> bool:
        return self.btc_trend_4h == "BULL"

    def btc_is_bearish(self) -> bool:
        return self.btc_trend_4h == "BEAR"

    def is_extreme_fear(self) -> bool:
        return self.fear_greed < 25

    def is_extreme_greed(self) -> bool:
        return self.fear_greed > 75

    def macro_warning(self) -> str:
        if self.macro_event_today:
            return f"⚠️ {self.macro_event_name} today — volatility risk"
        return ""

    def summary(self) -> str:
        """One-line market briefing for Telegram."""
        btc_icon = "🟢" if self.btc_is_bullish() else ("🔴" if self.btc_is_bearish() else "⚪")
        fg_icon  = "😱" if self.is_extreme_fear() else ("🤑" if self.is_extreme_greed() else "😐")
        dom_str  = f"{self.btc_dominance:.1f}%" if self.btc_dominance else "?"
        ls_str   = f"{self.ls_ratio:.2f}" if self.ls_ratio else "?"
        lines = [
            f"₿ BTC {btc_icon} `${self.btc_price:,.0f}` ({self.btc_change_24h:+.1f}% 24h) | 4H: `{self.btc_trend_4h}`",
            f"😨 Fear&Greed: `{self.fear_greed}` {fg_icon} *{self.fear_greed_label}* | BTC Dom: `{dom_str}`",
            f"⚖️ L/S Ratio: `{ls_str}` | OI Change: `{self.oi_change_pct:+.1f}%`",
        ]
        if self.macro_event_today:
            lines.append(f"🚨 *MACRO EVENT:* {self.macro_event_name} ({self.macro_event_impact} impact)")
        return "\n".join(lines)


# ─── Individual Fetchers ──────────────────────────────────────────────────────

async def fetch_btc_trend(exchange) -> dict:
    """
    Fetch BTC 4H and daily candles from Binance to determine trend direction.
    Uses EMA 20 vs EMA 50 logic same as engine.

    WHY THIS MATTERS:
      BTC is the market leader. ~80% of altcoins follow BTC direction.
      If BTC 4H is bearish, going LONG on altcoins is fighting the tide.
      This is the single most impactful global filter.
    """
    result = {
        "price": 0.0,
        "trend_4h": "NEUTRAL",
        "trend_daily": "NEUTRAL",
        "change_24h": 0.0,
    }
    try:
        # Fetch 4H and daily candles concurrently
        candles_4h, candles_daily = await asyncio.gather(
            exchange.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=60),
            exchange.fetch_ohlcv("BTC/USDT:USDT", "1d", limit=60),
        )

        import pandas as pd
        from ta.trend import EMAIndicator

        def trend_from_candles(candles):
            if not candles or len(candles) < 50:
                return "NEUTRAL", 0.0
            close = pd.Series([c[4] for c in candles])
            ema20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
            ema50 = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
            if ema20 > ema50 * 1.001:   # 0.1% buffer to avoid noise
                return "BULL", close.iloc[-1]
            elif ema20 < ema50 * 0.999:
                return "BEAR", close.iloc[-1]
            return "NEUTRAL", close.iloc[-1]

        trend_4h, price_4h = trend_from_candles(candles_4h)
        trend_daily, _     = trend_from_candles(candles_daily)

        # 24h change
        ticker = await exchange.fetch_ticker("BTC/USDT:USDT")
        change_24h = ticker.get("percentage") or 0.0

        result.update({
            "price"       : price_4h,
            "trend_4h"   : trend_4h,
            "trend_daily" : trend_daily,
            "change_24h"  : change_24h,
        })
        logger.info(f"BTC: ${price_4h:,.0f} | 4H:{trend_4h} | Daily:{trend_daily} | {change_24h:+.2f}%")
    except Exception as e:
        logger.warning(f"BTC trend fetch failed: {e}")
    return result


async def fetch_fear_and_greed() -> dict:
    """
    Crypto Fear & Greed Index — alternative.me (free, no API key).
    0-24 = Extreme Fear, 25-49 = Fear, 50-74 = Greed, 75-100 = Extreme Greed
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.alternative.me/fng/?limit=1") as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    d    = data["data"][0]
                    return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        logger.warning(f"Fear & Greed failed: {e}")
    return {"value": 50, "label": "Neutral"}


async def fetch_btc_dominance() -> float:
    """
    BTC dominance % from CoinGecko free API.

    WHY IT MATTERS:
      BTC dominance rising   = capital flowing INTO BTC, out of alts → bad for altcoin LONGs
      BTC dominance falling  = alt season, capital rotating to alts → great for altcoin LONGs
      Above 55% + rising     = dangerous to long alts
      Below 45% + falling    = peak alt season opportunity
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.coingecko.com/api/v3/global") as r:
                if r.status == 200:
                    data = await r.json()
                    dom  = data["data"]["market_cap_percentage"].get("btc", 0)
                    logger.info(f"BTC dominance: {dom:.1f}%")
                    return round(dom, 2)
    except Exception as e:
        logger.warning(f"BTC dominance fetch failed: {e}")
    return 0.0


async def fetch_long_short_ratio(exchange) -> float:
    """
    Binance long/short ratio for BTC — tells us how retail is positioned.

    WHY IT MATTERS:
      Ratio > 1.5 = crowd is heavily LONG → contrarian → SHORT pressure building
      Ratio < 0.7 = crowd is heavily SHORT → contrarian → LONG squeeze likely
      The crowd is usually wrong at extremes.
    """
    try:
        # Binance futures long/short ratio endpoint via ccxt
        # ccxt doesn't have a direct method so we use the raw API
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
            params = {"symbol": "BTCUSDT", "period": "4h", "limit": 1}
            async with s.get(url, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    ratio = float(data[0]["longShortRatio"])
                    logger.info(f"BTC L/S ratio: {ratio:.3f}")
                    return ratio
    except Exception as e:
        logger.warning(f"Long/short ratio fetch failed: {e}")
    return 1.0


async def fetch_open_interest_change(exchange) -> float:
    """
    Open Interest % change for BTC over last 4H from Binance.

    WHY IT MATTERS:
      OI rising + price rising   = real buyers entering → STRONG LONG signal
      OI rising + price falling  = short sellers entering → STRONG SHORT signal
      OI falling + any direction = position unwinding, trend may reverse
    """
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            url = "https://fapi.binance.com/futures/data/openInterestHist"
            params = {"symbol": "BTCUSDT", "period": "4h", "limit": 2}
            async with s.get(url, params=params) as r:
                if r.status == 200:
                    data  = await r.json()
                    if len(data) >= 2:
                        old_oi = float(data[0]["sumOpenInterestValue"])
                        new_oi = float(data[1]["sumOpenInterestValue"])
                        change = ((new_oi - old_oi) / old_oi) * 100 if old_oi else 0
                        logger.info(f"BTC OI change: {change:+.2f}%")
                        return round(change, 2)
    except Exception as e:
        logger.warning(f"OI change fetch failed: {e}")
    return 0.0


async def fetch_macro_events() -> dict:
    """
    High-impact economic events from ForexFactory public JSON calendar.
    Checks for FOMC, CPI, NFP, Fed speeches, interest rate decisions today.

    WHY IT MATTERS:
      FOMC day = expect 3-10% crypto swings. Most signals will be WRONG.
      CPI higher than expected = dollar strengthens = crypto dumps.
      NFP (Non-Farm Payrolls) = huge USD volatility = crypto volatility.
      Best practice: don't trade on FOMC/CPI day, wait for the dust to settle.
    """
    HIGH_IMPACT_KEYWORDS = [
        "FOMC", "Federal Reserve", "Fed Chair", "Interest Rate Decision",
        "CPI", "Consumer Price Index", "Inflation",
        "Non-Farm Payroll", "NFP", "Employment",
        "GDP", "Federal Funds Rate",
        "Jerome Powell", "Fed Meeting",
        "ETF", "Bitcoin ETF",
    ]

    result = {"event": False, "name": "", "impact": ""}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            # ForexFactory public JSON calendar
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
            async with s.get(url) as r:
                if r.status == 200:
                    events = await r.json(content_type=None)
                    for ev in events:
                        ev_date   = ev.get("date", "")[:10]
                        ev_impact = ev.get("impact", "")
                        ev_title  = ev.get("title", "")
                        ev_country = ev.get("country", "")

                        # Only check USD events (US economy moves crypto most)
                        if ev_country != "USD":
                            continue
                        if ev_date != today:
                            continue
                        if ev_impact not in ("High", "Medium"):
                            continue

                        # Check if it's a crypto-relevant macro event
                        for kw in HIGH_IMPACT_KEYWORDS:
                            if kw.lower() in ev_title.lower():
                                impact = "HIGH" if ev_impact == "High" else "MEDIUM"
                                result = {"event": True, "name": ev_title, "impact": impact}
                                logger.warning(f"MACRO EVENT TODAY: {ev_title} [{impact}]")
                                return result

                        # Even if not in our keyword list, flag ALL high-impact USD events
                        if ev_impact == "High":
                            result = {"event": True, "name": ev_title, "impact": "HIGH"}
                            logger.warning(f"HIGH IMPACT USD EVENT: {ev_title}")
                            return result
    except Exception as e:
        logger.warning(f"Macro calendar fetch failed: {e}")
    return result


async def fetch_crypto_news(coins: list[str], api_token: str = "") -> dict:
    """
    CryptoPanic news sentiment — free tier available at cryptopanic.com.
    Register at https://cryptopanic.com/developers/api/ for a free token.
    Add CRYPTOPANIC_TOKEN=your_token to your .env / Render env vars.

    WHY IT MATTERS:
      Positive news for a coin = real catalyst, increases win probability
      Negative news (hack, exploit, SEC action) = don't trade it, losses likely
      ETF approval news = huge LONG catalyst
      Exchange listing = short-term LONG catalyst

    If no token configured: skips gracefully, signals still work.
    """
    if not api_token:
        return {}  # Graceful skip if no token configured

    sentiment_map = {}
    headline_map  = {}

    try:
        # Batch by 5 coins per request to stay in free tier limits
        for i in range(0, min(len(coins), 20), 5):
            batch = ",".join(coins[i:i+5])
            url   = f"https://cryptopanic.com/api/free/v1/posts/?auth_token={api_token}&currencies={batch}&filter=important&public=true"

            async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()

            for post in data.get("results", []):
                currencies = [c["code"] for c in post.get("currencies", [])]
                votes      = post.get("votes", {})
                positive   = (votes.get("positive") or 0) + (votes.get("liked") or 0)
                negative   = (votes.get("negative") or 0) + (votes.get("disliked") or 0)
                important  = votes.get("important") or 0
                title      = post.get("title", "")[:80]

                for coin in currencies:
                    coin_upper = coin.upper()
                    if coin_upper not in coins:
                        continue
                    if coin_upper not in sentiment_map:
                        sentiment_map[coin_upper] = 0
                        headline_map[coin_upper]  = title

                    weight = 2 if important > 3 else 1
                    sentiment_map[coin_upper] += (positive - negative) * weight

            await asyncio.sleep(0.5)  # Rate limit

    except Exception as e:
        logger.warning(f"CryptoPanic fetch failed: {e}")

    # Convert raw scores to sentiment labels
    final = {}
    for coin, score in sentiment_map.items():
        if score > 3:
            final[coin] = "POSITIVE"
        elif score < -3:
            final[coin] = "NEGATIVE"
        else:
            final[coin] = "NEUTRAL"

    return final, headline_map


# ─── Main Assembler ───────────────────────────────────────────────────────────

async def build_market_context(
    exchange,
    scan_symbols : list[str],
    cryptopanic_token: str = "",
) -> MarketContext:
    """
    Assemble ALL market intelligence in parallel before scanning coins.
    Called ONCE per scan cycle — results shared across all 40 symbol analyses.

    This is far more efficient than fetching context data per coin.
    """
    ctx = MarketContext()
    ctx.fetched_at = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Extract base coin names for news lookup (BTC/USDT:USDT → BTC)
    coin_names = list({s.replace("/USDT:USDT", "").replace("/USDT", "") for s in scan_symbols})

    logger.info("Building market context (parallel fetch)...")

    # Fetch everything concurrently — parallel, not sequential
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

    # BTC trend
    if isinstance(btc_data, dict):
        ctx.btc_price       = btc_data.get("price", 0)
        ctx.btc_trend_4h    = btc_data.get("trend_4h", "NEUTRAL")
        ctx.btc_trend_daily = btc_data.get("trend_daily", "NEUTRAL")
        ctx.btc_change_24h  = btc_data.get("change_24h", 0)

    # Fear & Greed
    if isinstance(fg_data, dict):
        ctx.fear_greed       = fg_data.get("value", 50)
        ctx.fear_greed_label = fg_data.get("label", "Neutral")

    # BTC dominance
    if isinstance(btc_dom, float):
        ctx.btc_dominance = btc_dom

    # Long/Short ratio
    if isinstance(ls_ratio, float):
        ctx.ls_ratio = ls_ratio

    # OI change
    if isinstance(oi_change, float):
        ctx.oi_change_pct = oi_change

    # Macro events
    if isinstance(macro_data, dict) and macro_data.get("event"):
        ctx.macro_event_today  = True
        ctx.macro_event_name   = macro_data.get("name", "")
        ctx.macro_event_impact = macro_data.get("impact", "HIGH")
        ctx.warnings.append(f"MACRO EVENT: {ctx.macro_event_name}")

    # News sentiment
    if isinstance(news_data, tuple):
        sentiment, headlines = news_data
        ctx.news_sentiment = sentiment
        ctx.news_headlines = headlines

    logger.info(
        f"Market context ready | BTC:{ctx.btc_trend_4h} ${ctx.btc_price:,.0f} | "
        f"F&G:{ctx.fear_greed} | Dom:{ctx.btc_dominance:.1f}% | "
        f"L/S:{ctx.ls_ratio:.2f} | OI:{ctx.oi_change_pct:+.1f}% | "
        f"Macro:{ctx.macro_event_today}"
    )
    return ctx
