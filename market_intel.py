# market_intel.py
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


async def fetch_btc_price_direct() -> float:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://fapi.binance.com/fapi/v1/ticker/price",
                             params={"symbol": "BTCUSDT"}) as r:
                if r.status == 200:
                    return float((await r.json())["price"])
    except Exception as e:
        logger.warning(f"BTC direct price: {e}")
    return 0.0


async def fetch_btc_trend(exchange) -> dict:
    result = {"price": 0.0, "trend_4h": "NEUTRAL", "trend_daily": "NEUTRAL", "change_24h": 0.0}
    try:
        import pandas as pd
        from ta.trend import EMAIndicator
        price_task  = fetch_btc_price_direct()
        c4h_task    = exchange.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=60)
        c1d_task    = exchange.fetch_ohlcv("BTC/USDT:USDT", "1d", limit=60)
        price, c4h, c1d = await asyncio.gather(price_task, c4h_task, c1d_task, return_exceptions=True)
        if isinstance(price, float) and price > 0:
            result["price"] = price

        def trend(candles):
            if not isinstance(candles, list) or len(candles) < 50:
                return "NEUTRAL"
            close = pd.Series([float(c[4]) for c in candles])
            e20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
            e50 = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
            if result["price"] == 0:
                result["price"] = float(close.iloc[-1])
            if e20 > e50 * 1.001:   return "BULL"
            elif e20 < e50 * 0.999: return "BEAR"
            return "NEUTRAL"

        result["trend_4h"]    = trend(c4h)
        result["trend_daily"] = trend(c1d)

        try:
            t = await exchange.fetch_ticker("BTC/USDT:USDT")
            result["change_24h"] = t.get("percentage", 0) or 0
        except Exception:
            pass

        if result["price"] == 0:
            result["price"] = await fetch_btc_price_direct()

    except Exception as e:
        logger.warning(f"BTC trend: {e}")
        if result["price"] == 0:
            result["price"] = await fetch_btc_price_direct()
    return result


async def fetch_fear_greed() -> dict:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.alternative.me/fng/?limit=1") as r:
                if r.status == 200:
                    d = (await r.json(content_type=None))["data"][0]
                    return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        logger.warning(f"F&G: {e}")
    return {"value": 50, "label": "Neutral"}


async def fetch_btc_dominance() -> float:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://api.coingecko.com/api/v3/global") as r:
                if r.status == 200:
                    return round((await r.json())["data"]["market_cap_percentage"].get("btc", 0), 2)
    except Exception as e:
        logger.warning(f"BTC dom: {e}")
    return 0.0


async def fetch_ls_ratio() -> float:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                             params={"symbol": "BTCUSDT", "period": "4h", "limit": 1}) as r:
                if r.status == 200:
                    return float((await r.json())[0]["longShortRatio"])
    except Exception as e:
        logger.warning(f"L/S: {e}")
    return 1.0


async def fetch_oi_change() -> float:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://fapi.binance.com/futures/data/openInterestHist",
                             params={"symbol": "BTCUSDT", "period": "4h", "limit": 2}) as r:
                if r.status == 200:
                    d = await r.json()
                    if len(d) >= 2:
                        old = float(d[0]["sumOpenInterestValue"])
                        new = float(d[1]["sumOpenInterestValue"])
                        return round(((new - old) / old) * 100 if old else 0, 2)
    except Exception as e:
        logger.warning(f"OI: {e}")
    return 0.0


async def fetch_macro_events() -> dict:
    HIGH = ["FOMC","Federal Reserve","Interest Rate","CPI","Consumer Price",
            "Non-Farm","NFP","GDP","Powell","PCE","Inflation"]
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json") as r:
                if r.status == 200:
                    for ev in (await r.json(content_type=None)):
                        if ev.get("country") != "USD": continue
                        if ev.get("date","")[:10] != today: continue
                        if ev.get("impact") not in ("High","Medium"): continue
                        title  = ev.get("title","")
                        impact = "HIGH" if ev["impact"] == "High" else "MEDIUM"
                        for kw in HIGH:
                            if kw.lower() in title.lower():
                                return {"event": True, "name": title, "impact": impact}
                        if ev["impact"] == "High":
                            return {"event": True, "name": title, "impact": "HIGH"}
    except Exception as e:
        logger.warning(f"Macro: {e}")
    return {"event": False, "name": "", "impact": ""}


async def build_market_context(exchange, scan_symbols, cryptopanic_token="") -> MarketContext:
    ctx            = MarketContext()
    ctx.fetched_at = datetime.now(timezone.utc).strftime("%H:%M UTC")

    results = await asyncio.gather(
        fetch_btc_trend(exchange),
        fetch_fear_greed(),
        fetch_btc_dominance(),
        fetch_ls_ratio(),
        fetch_oi_change(),
        fetch_macro_events(),
        return_exceptions=True,
    )
    btc, fg, dom, ls, oi, macro = results

    if isinstance(btc, dict):
        ctx.btc_price       = btc.get("price", 0)
        ctx.btc_trend_4h    = btc.get("trend_4h", "NEUTRAL")
        ctx.btc_trend_daily = btc.get("trend_daily", "NEUTRAL")
        ctx.btc_change_24h  = btc.get("change_24h", 0)
    if isinstance(fg, dict):
        ctx.fear_greed       = fg.get("value", 50)
        ctx.fear_greed_label = fg.get("label", "Neutral")
    if isinstance(dom, float) and dom > 0:
        ctx.btc_dominance = dom
    if isinstance(ls, float) and ls > 0:
        ctx.ls_ratio = ls
    if isinstance(oi, float):
        ctx.oi_change_pct = oi
    if isinstance(macro, dict) and macro.get("event"):
        ctx.macro_event_today  = True
        ctx.macro_event_name   = macro.get("name", "")
        ctx.macro_event_impact = macro.get("impact", "HIGH")

    if ctx.btc_price == 0:
        ctx.btc_price = await fetch_btc_price_direct()

    logger.info(f"Context: BTC ${ctx.btc_price:,.0f} | 4H:{ctx.btc_trend_4h} | "
                f"F&G:{ctx.fear_greed} | L/S:{ctx.ls_ratio:.2f}")
    return ctx
