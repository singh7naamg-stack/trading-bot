# ============================================================
#  QuestLife Signal Bot — market_intel.py  v5.0
#  Free market intelligence — assembled once per scan cycle
# ============================================================

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp
import ccxt.async_support as ccxt

logger  = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=8)


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


async def fetch_btc_trend(exchange):
    result = {"price": 0.0, "trend_4h": "NEUTRAL", "trend_daily": "NEUTRAL", "change_24h": 0.0}
    try:
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
            if ema20 > ema50 * 1.001:   return "BULL", float(close.iloc[-1])
            elif ema20 < ema50 * 0.999: return "BEAR", float(close.iloc[-1])
            return "NEUTRAL", float(close.iloc[-1])

        trend_4h, price  = trend_from_candles(candles_4h)
        trend_daily, _   = trend_from_candles(candles_daily)
        ticker           = await exchange.fetch_ticker("BTC/USDT:USDT")
        result.update({"price": price, "trend_4h": trend_4h,
                        "trend_daily": trend_daily, "change_24h": ticker.get("percentage", 0) or 0})
        logger.info(f"BTC ${price:,.0f} | 4H:{trend_4h} | Daily:{trend_daily}")
    except Exception as e:
        logger.warning(f"BTC trend failed: {e}")
    return result


async def fetch_fear_and_greed():
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.alternative.me/fng/?limit=1") as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    d    = data["data"][0]
                    return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        logger.warning(f"F&G failed: {e}")
    return {"value": 50, "label": "Neutral"}


async def fetch_btc_dominance():
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.coingecko.com/api/v3/global") as r:
                if r.status == 200:
                    data = await r.json()
                    return round(data["data"]["market_cap_percentage"].get("btc", 0), 2)
    except Exception as e:
        logger.warning(f"BTC dom failed: {e}")
    return 0.0


async def fetch_long_short_ratio(exchange):
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": "BTCUSDT", "period": "4h", "limit": 1}
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return float(data[0]["longShortRatio"])
    except Exception as e:
        logger.warning(f"L/S ratio failed: {e}")
    return 1.0


async def fetch_open_interest_change(exchange):
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": "BTCUSDT", "period": "4h", "limit": 2}
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if len(data) >= 2:
                        old = float(data[0]["sumOpenInterestValue"])
                        new = float(data[1]["sumOpenInterestValue"])
                        return round(((new - old) / old) * 100 if old else 0, 2)
    except Exception as e:
        logger.warning(f"OI change failed: {e}")
    return 0.0


async def fetch_macro_events():
    HIGH_IMPACT = [
        "FOMC","Federal Reserve","Fed Chair","Interest Rate","CPI","Consumer Price",
        "Non-Farm","NFP","Employment","GDP","Jerome Powell","Fed Meeting","ETF","Bitcoin ETF"
    ]
    result = {"event": False, "name": "", "impact": ""}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json") as r:
                if r.status == 200:
                    events = await r.json(content_type=None)
                    for ev in events:
                        if ev.get("country") != "USD": continue
                        if ev.get("date","")[:10] != today: continue
                        if ev.get("impact") not in ("High","Medium"): continue
                        title  = ev.get("title","")
                        impact = "HIGH" if ev["impact"] == "High" else "MEDIUM"
                        for kw in HIGH_IMPACT:
                            if kw.lower() in title.lower():
                                return {"event": True, "name": title, "impact": impact}
                        if ev["impact"] == "High":
                            return {"event": True, "name": title, "impact": "HIGH"}
    except Exception as e:
        logger.warning(f"Macro events failed: {e}")
    return result


async def fetch_crypto_news(coins, api_token=""):
    if not api_token:
        return {}, {}
    sentiment_map, headline_map = {}, {}
    try:
        for i in range(0, min(len(coins), 20), 5):
            batch = ",".join(coins[i:i+5])
            url   = (f"https://cryptopanic.com/api/free/v1/posts/"
                     f"?auth_token={api_token}&currencies={batch}&filter=important&public=true")
            async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                async with s.get(url) as r:
                    if r.status != 200: continue
                    data = await r.json()
            for post in data.get("results", []):
                currencies = [c["code"] for c in post.get("currencies", [])]
                votes      = post.get("votes", {})
                pos        = (votes.get("positive") or 0) + (votes.get("liked") or 0)
                neg        = (votes.get("negative") or 0) + (votes.get("disliked") or 0)
                imp        = votes.get("important") or 0
                title      = post.get("title","")[:80]
                for coin in currencies:
                    cu = coin.upper()
                    if cu not in coins: continue
                    if cu not in sentiment_map:
                        sentiment_map[cu] = 0
                        headline_map[cu]  = title
                    sentiment_map[cu] += (pos - neg) * (2 if imp > 3 else 1)
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning(f"CryptoPanic failed: {e}")
    final = {}
    for coin, s in sentiment_map.items():
        final[coin] = "POSITIVE" if s > 3 else ("NEGATIVE" if s < -3 else "NEUTRAL")
    return final, headline_map


async def build_market_context(exchange, scan_symbols, cryptopanic_token=""):
    ctx            = MarketContext()
    ctx.fetched_at = datetime.now(timezone.utc).strftime("%H:%M UTC")
    coin_names     = list({s.replace("/USDT:USDT","").replace("/USDT","") for s in scan_symbols})
    logger.info("Building market context...")
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

    if isinstance(btc_data, dict):
        ctx.btc_price       = btc_data.get("price", 0)
        ctx.btc_trend_4h    = btc_data.get("trend_4h", "NEUTRAL")
        ctx.btc_trend_daily = btc_data.get("trend_daily", "NEUTRAL")
        ctx.btc_change_24h  = btc_data.get("change_24h", 0)
    if isinstance(fg_data,   dict):  ctx.fear_greed = fg_data.get("value",50); ctx.fear_greed_label = fg_data.get("label","Neutral")
    if isinstance(btc_dom,   float): ctx.btc_dominance = btc_dom
    if isinstance(ls_ratio,  float): ctx.ls_ratio      = ls_ratio
    if isinstance(oi_change, float): ctx.oi_change_pct = oi_change
    if isinstance(macro_data, dict) and macro_data.get("event"):
        ctx.macro_event_today  = True
        ctx.macro_event_name   = macro_data.get("name","")
        ctx.macro_event_impact = macro_data.get("impact","HIGH")
    if isinstance(news_data, tuple):
        ctx.news_sentiment, ctx.news_headlines = news_data
    logger.info(f"Context ready: BTC {ctx.btc_trend_4h} ${ctx.btc_price:,.0f} | F&G:{ctx.fear_greed} | Dom:{ctx.btc_dominance:.1f}%")
    return ctx
